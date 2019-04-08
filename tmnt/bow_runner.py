# coding: utf-8

import math
import logging
import datetime
import io
import os
import json
import mxnet as mx
import numpy as np
import pickle

from mxnet import autograd
from mxnet import gluon
import gluonnlp as nlp
from pathlib import Path

from tmnt.bow_vae.bow_doc_loader import DataIterLoader, collect_sparse_data, BowDataSet, collect_stream_as_sparse_matrix
from tmnt.bow_vae.bow_models import BowNTM, MetaDataBowNTM
from tmnt.bow_vae.topic_seeds import get_seed_matrix_from_file
from tmnt.utils.log_utils import logging_config
from tmnt.utils.mat_utils import export_sparse_matrix, export_vocab
from tmnt.utils.random import seed_rng
from tmnt.coherence.npmi import EvaluateNPMI
from tmnt.modsel.configuration import TMNTConfig

import ConfigSpace as CS
import ConfigSpace.hyperparameters as CSH
from hpbandster.core.worker import Worker
import hpbandster.core.nameserver as hpns
import hpbandster.core.result as hpres
from hpbandster.optimizers import BOHB as BOHB


def get_wd_freqs(data_csr, max_sample_size=10000):
    sample_size = min(max_sample_size, data_csr.shape[0])
    data = data_csr[:sample_size].asnumpy() # only take first 10000 to estimate frequencies - but should select at RANDOM
    sums = np.sum(data, axis=0)
    return list(sums)


def evaluate(model, data_loader, total_words, args, ctx=mx.cpu(), debug=False):
    total_rec_loss = 0
    for i, (data,labels) in enumerate(data_loader):
        if labels is None:            
            labels = mx.nd.expand_dims(mx.nd.zeros(data.shape[0]), 1)
            labels = labels.as_in_context(ctx)
        data = data.as_in_context(ctx)
        _, kl_loss, rec_loss, _, _, _, log_out = model(data, labels) if args.use_labels_as_covars else model(data)
        total_rec_loss += rec_loss.sum().asscalar()
    perplexity = math.exp(total_rec_loss / total_words)
    logging.info("TEST/VALIDATION Perplexity = {}".format(perplexity))
    return perplexity


def compute_coherence(model, vocab, num_topics, k, test_data):
    w = model.decoder.collect_params().get('weight').data()
    sorted_ids = w.argsort(axis=0, is_ascend=False)
    num_topics = min(num_topics, sorted_ids.shape[-1])
    top_k_words_per_topic = [[int(i) for i in list(sorted_ids[:k, t].asnumpy())] for t in range(num_topics)]
    npmi_eval = EvaluateNPMI(top_k_words_per_topic)
    npmi = npmi_eval.evaluate_csr_mat(test_data)
    logging.info("Test Coherence: {}".format(npmi))
    return npmi


def analyze_seed_matrix(model, seed_matrix):
    w = model.decoder.collect_params().get('weight').data()
    ts = mx.nd.take(w, seed_matrix)   ## should have shape (T', S', T)
    ts_sums = mx.nd.sum(ts, axis=1)
    ts_probs = mx.nd.softmax(ts_sums)
    print("ts_prob = {}".format(ts_probs))
    entropies = -mx.nd.sum(ts_probs * mx.nd.log(ts_probs), axis=1)
    print("entropies = {}".format(entropies))
    seed_means = mx.nd.mean(ts, axis=1)  # (G,K)
    seed_pr = mx.nd.softmax(seed_means)
    per_topic_entropy_1 = -mx.nd.sum(seed_pr * mx.nd.log(seed_pr), axis=0)
    print("per_topic_entropy = {}".format(per_topic_entropy_1))
    total_topic_sum_means = mx.nd.sum(seed_means, axis=0)
    print("sum means = {}".format(total_topic_sum_means))    
    per_topic_entropy = mx.nd.sum(per_topic_entropy_1 * total_topic_sum_means)
    

def log_top_k_words_per_topic(model, vocab, num_topics, k):
    w = model.decoder.collect_params().get('weight').data()
    sorted_ids = w.argsort(axis=0, is_ascend=False)
    for t in range(num_topics):
        top_k = [ vocab.idx_to_token[int(i)] for i in list(sorted_ids[:k, t].asnumpy()) ]
        term_str = ' '.join(top_k)
        logging.info("Topic {}: {}".format(str(t), term_str))


class BowVAEWorker(Worker):
    def __init__(self, c_args, vocabulary, data_train_csr, total_tr_words,
                 data_test_csr, total_tst_words, train_labels=None, test_labels=None, ctx=mx.cpu(), max_budget=32, **kwargs):
        super().__init__(**kwargs)
        self.c_args = c_args
        self.total_tr_words = total_tr_words
        self.total_tst_words = total_tst_words
        self.vocabulary = vocabulary
        self.ctx = ctx
        self.data_test_csr = data_test_csr
        self.max_budget = max_budget
        
        self.wd_freqs = get_wd_freqs(data_train_csr)
        self.emb_size = vocabulary.embedding.idx_to_vec[0].size if vocabulary.embedding else None
        
        self.seed_matrix = None
        if c_args.topic_seed_file:
            self.seed_matrix = get_seed_matrix_from_file(c_args.topic_seed_file, vocabulary)
        train_iter = mx.io.NDArrayIter(data_train_csr, train_labels, c_args.batch_size, last_batch_handle='discard', shuffle=True)
        self.train_dataloader = DataIterLoader(train_iter)    
        test_iter = mx.io.NDArrayIter(data_test_csr, test_labels, c_args.batch_size, last_batch_handle='discard', shuffle=False)
        self.test_dataloader = DataIterLoader(test_iter)


    def _train_model(self, config, budget):
        logging.info("Evaluating with budget {} against config: {}".format(budget, config))
        lr = config['lr']
        latent_distrib = config['latent_distribution']
        optimizer = config['optimizer']
        n_latent = int(config['n_latent'])
        enc_hidden_dim = int(config['enc_hidden_dim'])
        emb_size = self.emb_size or int(config['embedding_size'])
        target_sparsity = float(config.get('target_sparsity', 0.0))
        coherence_reg_penalty = float(config.get('coherence_regularizer_penalty', 0.0))

        l1_coef = c_args.init_sparsity_pen if target_sparsity > 0.0 else 0.0
        
        if self.c_args.use_labels_as_covars and train_labels is not None:
            n_covars = mx.nd.max(train_labels).asscalar() + 1
            train_labels = mx.nd.one_hot(train_labels, n_covars)
            test_labels = mx.nd.one_hot(test_labels, n_covars) if test_labels is not None else None
            model = \
                MetaDataBowNTM(n_covars, self.vocabulary, enc_hidden_dim, n_latent, emb_size,
                               fixed_embedding=self.c_args.fixed_embedding, latent_distrib=self.c_args.latent_distribution,
                               init_l1=l1_coef, coherence_reg_penalty=coherence_reg_penalty,
                               batch_size=self.c_args.batch_size, wd_freqs=wd_freqs, ctx=ctx)
        else:
            model = \
                BowNTM(self.vocabulary, enc_hidden_dim, n_latent, emb_size,
                   fixed_embedding=self.c_args.fixed_embedding, latent_distrib=latent_distrib,
                   init_l1=l1_coef, coherence_reg_penalty=coherence_reg_penalty,
                   batch_size=self.c_args.batch_size, wd_freqs=self.wd_freqs, seed_mat=self.seed_matrix, ctx=self.ctx)
        trainer = gluon.Trainer(model.collect_params(), optimizer, {'learning_rate': lr})
        for epoch in range(int(budget)):
            epoch_loss = 0
            total_rec_loss = 0
            total_l1_pen = 0
            total_kl_loss = 0
            total_entropies_loss = 0
            total_coherence_loss = 0
            tr_size = 0
            for i, (data, labels) in enumerate(self.train_dataloader):
                tr_size += data.shape[0]
                if labels is None or labels.size == 0:
                    labels = mx.nd.expand_dims(mx.nd.zeros(data.shape[0]), 1)
                    labels = labels.as_in_context(self.ctx)
                data = data.as_in_context(self.ctx)
                with autograd.record():
                    elbo, kl_loss, rec_loss, l1_pen, entropies, coherence_loss, _ = model(data, labels) if self.c_args.use_labels_as_covars else model(data)
                    elbo_mean = elbo.mean()
                elbo_mean.backward()
                trainer.step(data.shape[0]) ## step based on batch size
                total_kl_loss += kl_loss.sum().asscalar()
                total_l1_pen += l1_pen.sum().asscalar()
                total_rec_loss += rec_loss.sum().asscalar()
                if coherence_loss is not None:
                    total_coherence_loss += coherence_loss
                if entropies is not None:
                    total_entropies_loss += entropies.sum().asscalar()
                epoch_loss += elbo.sum().asscalar()
                
            logging.info("Epoch {}: Loss = {}, [ KL loss = {:8.4f} ] [ L1 loss = {:8.4f} ] [ Rec loss = {:8.4f}] [ Coherence loss = {:8.4f} ] [ Entropy losss = {:8.4f} ]".
                     format(epoch,
                            epoch_loss / tr_size,
                            total_kl_loss / tr_size,
                            total_l1_pen / tr_size,
                            total_rec_loss / tr_size,
                            total_coherence_loss / tr_size,                             
                            total_entropies_loss / tr_size))
            if target_sparsity > 0.0:            
                dec_weights = model.decoder.collect_params().get('weight').data().abs()
                ratio_small_weights = (dec_weights < self.c_args.sparsity_threshold).sum().asscalar() / dec_weights.size
                l1_coef = l1_coef * math.pow(2.0, self.c_args.target_sparsity - ratio_small_weights)
                logging.info("Setting L1 coeffficient to {} [sparsity ratio = {}]".format(l1_coef, ratio_small_weights))
                model.l1_pen_const.set_data(mx.nd.array([l1_coef]))
            if self.data_test_csr is not None and (epoch + 1) % self.c_args.eval_freq == 0:
                perplexity = evaluate(model, self.test_dataloader, self.total_tst_words, self.c_args, self.ctx)
                if self.c_args.trace_file:
                    otype = 'a+' if epoch >= self.c_args.eval_freq else 'w+'
                    with io.open(self.c_args.trace_file, otype) as fp:
                        if otype == 'w+':
                            fp.write("Epoch,PPL,NPMI\n")
                        npmi = compute_coherence(model, self.vocabulary, n_latent, 10, self.data_test_csr)
                        fp.write("{:3d},{:10.2f},{:8.4f}\n".format(epoch, perplexity, npmi))
        perplexity = evaluate(model, self.test_dataloader, self.total_tst_words, self.c_args, self.ctx)
        npmi = compute_coherence(model, self.vocabulary, n_latent, 10, self.data_test_csr)
        logging.info("NPMI = {}".format(npmi))
        res = {
            'loss': 1.0 - npmi,
            'info': {
                'test_perplexity': perplexity,
                'train_loss': (epoch_loss / tr_size)
            }
        }
        return model, res


    def compute(self, config, budget, working_directory, *args, **kwargs):
        _, res = self._train_model(config, budget)
        return res

    def retrain_best_config(self, config, budget):
        model, _ = self._train_model(config, budget)
        write_model(model, config, budget, self.emb_size, self.vocabulary, self.c_args)
        

def select_model(worker, tmnt_config_space, result_logger):
    tmnt_config = TMNTConfig(tmnt_config_space)
    worker.run(background=True)
    cs = tmnt_config.get_configspace() 
    config = cs.sample_configuration().get_dictionary()
    logging.info(config)
    bohb = BOHB(  configspace = cs,
                  run_id = '0', nameserver='127.0.0.1', result_logger=result_logger,
                  min_budget=2, max_budget=8
           )
    res = bohb.run(n_iterations=4)
    bohb.shutdown(shutdown_workers=True)
    return res

def write_model(m, config, budget, emb_size, vocab, args):
    if args.model_dir:
        pfile = os.path.join(args.model_dir, 'model.params')
        sp_file = os.path.join(args.model_dir, 'model.specs')
        vocab_file = os.path.join(args.model_dir, 'vocab.json')
        m.save_parameters(pfile)
        ## if the embedding_size wasn't set explicitly (e.g. determined via pre-trained embedding), then set it here
        if emb_size is not None:
            config['embedding_size'] = emb_size
        config['training_epochs'] = int(budget)
        specs = json.dumps(config)
        with open(sp_file, 'w') as f:
            f.write(specs)
        with open(vocab_file, 'w') as f:
            f.write(vocab.to_json())

def train_bow_vae(args):
    i_dt = datetime.datetime.now()
    train_out_dir = '{}/train_{}_{}_{}_{}_{}_{}'.format(args.save_dir,i_dt.year,i_dt.month,i_dt.day,i_dt.hour,i_dt.minute,i_dt.second)
    logging_config(folder=train_out_dir, name='bow_ntm', level=logging.INFO)
    logging.info(args)
    seed_rng(args.seed)
    sp_vec_data = False
    ## if the vocab file and training files are available, use those
    if args.vocab_file and args.tr_vec_file:
        vpath = Path(args.vocab_file)
        tpath = Path(args.tr_vec_file)
        if vpath.is_file() and tpath.is_file():
            sp_vec_data = True
    if sp_vec_data:
        logging.info("Loading data via pre-computed vocabulary and sparse vector format document representation")
        vocab, tr_csr_mat, total_tr_words, tst_csr_mat, total_tst_words, tr_labels, tst_labels = \
            collect_sparse_data(args.tr_vec_file, args.vocab_file, args.tst_vec_file)
    else:
        logging.info("Loading and pre-processing text data found in {}".format(args.train_dir))
        tr_dataset = BowDataSet(args.train_dir, args.file_pat)    
        tr_csr_mat, vocab, total_tr_words = collect_stream_as_sparse_matrix(tr_dataset, max_vocab_size=args.max_vocab_size)
        tr_labels = None
        tst_labels = None
        if args.vocab_file and args.tr_vec_file:
            export_sparse_matrix(tr_csr_mat, args.tr_vec_file)
            export_vocab(vocab, args.vocab_file)
        if args.test_dir:
            tst_dataset = BowDataSet(args.test_dir, args.file_pat)
            tst_csr_mat, _, total_tst_words = collect_stream_as_sparse_matrix(tst_dataset, pre_vocab=vocab)
            if args.vocab_file and args.tst_vec_file:
                export_sparse_matrix(tst_csr_mat, args.tst_vec_file)
    ctx = mx.cpu() if args.gpu is None or args.gpu == '' or int(args.gpu) < 0 else mx.gpu(int(args.gpu))
    if args.embedding_source:
        glove_twitter = nlp.embedding.create('glove', source=args.embedding_source)
        vocab.set_embedding(glove_twitter)
        emb_size = len(vocab.embedding.idx_to_vec[0])
        num_oov = 0
        for word in vocab.embedding._idx_to_token:
            if (vocab.embedding[word] == mx.nd.zeros(emb_size)).sum() == emb_size:
                logging.info("Term {} is OOV".format(word))
                num_oov += 1
                vocab.embedding[word] = mx.nd.random.normal(0, 1.0, emb_size)
        logging.info(">> {} Words did not appear in embedding source {}".format(num_oov, args.embedding_source))
    ### XXX - NOTE: For smaller datasets, may make sense to convert sparse matrices to dense here up front
    if args.model_select:
        result_logger = hpres.json_result_logger(directory=args.save_dir, overwrite=True)
        worker = BowVAEWorker(args, vocab, tr_csr_mat, total_tr_words, tst_csr_mat, total_tst_words, tr_labels, tst_labels, ctx=ctx,
                              max_budget=args.epochs,
                              nameserver='127.0.0.1', run_id='0')
        NS = hpns.NameServer(run_id='0', host='127.0.0.1', port=None)
        NS.start()
        res = select_model(worker, args.config_space, result_logger)
        NS.shutdown()
        id2config = res.get_id2config_mapping()
        incumbent = res.get_incumbent_id()
        logging.info('Best found configuration:', id2config[incumbent]['config'])
        logging.info('Total budget corresponds to %.1f full function evaluations.'%(sum([r.budget for r in res.get_all_runs()])/32))
        # Here is how you get he incumbent (best configuration)
        # let's grab the run on the highest budget
        inc_runs = res.get_runs_by_id(incumbent)
        inc_run = inc_runs[-1]
        inc_loss = inc_run.loss
        inc_config = id2config[incumbent]['config']
        logging.info("Best configuration loss = {}".format(inc_loss))
        logging.info("Best configuration {}".format(inc_config))
        with open(os.path.join(args.save_dir, 'results.pkl'), 'wb') as fh:
            pickle.dump(res, fh)
        if args.model_dir:
            worker.retrain_best_config(inc_config, inc_run.budget)
    elif args.config_instance:
        worker = BowVAEWorker(args, vocab, tr_csr_mat, total_tr_words, tst_csr_mat, total_tst_words, tr_labels, tst_labels, ctx=ctx,
                              max_budget=args.epochs, run_id='0')
        with open(args.config_instance, 'r') as f:
            config = json.loads(f.read())
        worker.retrain_best_config(config, int(config['training_epochs']))
    else:
        logging.error("Expected model selection with config space definition OR a specific configuration instance. See documentation.")
