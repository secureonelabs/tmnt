# coding: utf-8

"""
File/module contains routines for loading in text documents to sparse matrix representations
for efficient neural variational model training.
"""

import mxnet as mx
import mxnet.ndarray as F
import codecs
import itertools
import gluonnlp as nlp
import functools
import warnings
import os

from gluonnlp.data import SimpleDatasetStream, CorpusDataset


def preprocess_dataset_stream(stream, min_freq=3, max_vocab_size=None):
    counter = None
    for data in iter(stream):
        counter = nlp.data.count_tokens(itertools.chain.from_iterable(data), counter = counter)
        #logging.info('.. counter size = {} ..'.format(str(len(counter))))
    vocab = nlp.Vocab(counter, unknown_token=None, padding_token=None,
                          bos_token=None, eos_token=None, min_freq=min_freq,
                          max_size=max_vocab_size)
    idx_to_counts = [counter[w] for w in vocab.idx_to_token]

    def code(doc):
        """
        Parameters
        ----------
        Token sequence for all tokens in a file/document

        Returns
        -------
        Token ids with associated frequencies (sparse vector)
        """
        ## just drop out of vocab items
        doc_tok_ids = [vocab[token] for token in doc if token in vocab]
        doc_counter = nlp.data.count_tokens(doc_tok_ids)        
        return sorted(doc_counter.items())

    def code_corpus(corpus):
        return corpus.transform(code)

    stream = stream.transform(code_corpus) 
    return stream, vocab, idx_to_counts


def collect_stream_as_sparse_matrix(stream, min_freq=3, max_vocab_size=None):
    strm, vocab, idx_to_counts = preprocess_dataset_stream(stream, min_freq, max_vocab_size)
    indices = []
    values = []
    indptrs = [0]
    cumulative = 0
    ndocs = 0
    #all_toks = []
    for i,doc in enumerate(strm):
        ndocs += 1
        doc_toks = list(doc)[0]
        #all_toks.append(doc_toks)
        inds, vs = zip(*doc_toks)
        ln = len(doc_toks)
        cumulative += ln
        indptrs.append(cumulative)
        values.extend(vs)
        indices.extend(inds)
    # can use this with NDArrayIter
    # dataiter = mx.io.NDArrayIter(data, labels, batch_size, last_batch_handle='discard')
    ## inspect - [ batch.data[0] for batch in dataiter ]
    return mx.nd.sparse.csr_matrix((values, indices, indptrs), shape = (ndocs, len(vocab)))
    
        
    

class BowDataSet(SimpleDatasetStream):
    def __init__(self, root, pattern, bos, eos, skip_empty):
        self._root = root
        self._file_pattern = os.path.join(root, pattern)
        self.codec = 'utf-8'
        super(BowDataSet, self).__init__(
            dataset=CorpusDataset,
            file_pattern = self._file_pattern,
            file_sampler='random',
            sample_splitter=NullSplitter())


class NullSplitter(nlp.data.Splitter):

    def __call__(self, s):
        return [s]
        
