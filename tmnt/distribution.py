#coding: utf-8
# Copyright (c) 2019-2021 The MITRE Corporation.
"""
Variational latent distributions (e.g. Gaussian, Logistic Gaussian)
"""

import math
import mxnet as mx
import numpy as np
from mxnet import gluon as nn
from torch import nn
from torch.distributions.normal import Normal
from torch.distributions.uniform import Uniform
from scipy import special as sp


__all__ = ['GaussianDistribution', 'GaussianUnitVarDistribution', 'LogisticGaussianDistribution', 'HyperSphericalDistribution']


class BaseDistribution(nn.Module):
    
    def __init__(self, enc_size, n_latent, device):
        super(BaseDistribution, self).__init__()
        self.n_latent = n_latent
        self.device = device
        self.mu_encoder = nn.Linear(enc_size, n_latent)
        self.mu_bn = nn.BatchNorm(n_latent, momentum = 0.8, epsilon=0.0001)
        self.softmax = nn.Softmax(dim=n_latent)        
        #self.mu_bn.collect_params().setattr('grad_req', 'null')

    ## perform any postinitialization setup
    def post_init(self, ctx):
        pass

    ## this is required by most priors
    def _get_gaussian_sample(self, mu, lv, batch_size):
        #eps = F.random_normal(loc=0, scale=1, shape=(batch_size, self.n_latent), ctx=self.model_ctx)
        eps = Normal(nn.zeros(batch_size, self.n_latent), nn.ones(batch_size, self.n_latent)).sample()
        return mu + torch.exp(0.5*lv) * eps

    ## this is required by most priors
    def _get_unit_var_gaussian_sample(self, mu, batch_size):
        eps = Normal(nn.zeros(batch_size, self.n_latent), nn.ones(batch_size, self.n_latent)).sample()
        return mu + eps

    def get_mu_encoding(self, data, include_bn=False):
        """Provide the distribution mean as the natural result of running the full encoder
        
        Parameters:
            data (:class:`mxnet.ndarray.NDArray`): Output of pre-latent encoding layers
        Returns:
            encoding (:class:`mxnet.ndarray.NDArray`): Encoding vector representing unnormalized topic proportions
        """
        enc = self.mu_encoder(data)
        if include_bn:
            return self.mu_bn(enc)
        else:
            return enc


class GaussianDistribution(BaseDistribution):
    """Gaussian latent distribution with diagnol co-variance.

    Parameters:
        n_latent (int): Dimentionality of the latent distribution
        ctx (mxnet.context.Context): Mxnet computational context (cpu or gpu[id])
        dr (float): Dropout value for dropout applied post sample. optional (default = 0.2)
    """
    def __init__(self, enc_size, n_latent, device='cpu', dr=0.2):
        super(GaussianDistribution, self).__init__(enc_size, n_latent, device)
        self.lv_encoder = nn.Linear(enc_size, n_latent)            
        self.lv_bn = nn.BatchNorm(n_latent, momentum = 0.8, epsilon=0.001)
        self.post_sample_dr_o = nn.Dropout(p=dr)        

    def _get_kl_term(self, mu, lv):
        return -0.5 * torch.sum(1 + lv - mu*mu - torch.exp(lv), 1)

    def hybrid_forward(self, data, batch_size):
        """Generate a sample according to the Gaussian given the encoder outputs
        """
        mu = self.mu_encoder(data)
        mu_bn = self.mu_bn(mu)
        lv = self.lv_encoder(data)
        lv_bn = self.lv_bn(lv)
        z = self._get_gaussian_sample(mu_bn, lv_bn, batch_size)
        KL = self._get_kl_term(mu_bn, lv_bn)
        z = self.post_sample_dr_o(z)
        return z, KL


class GaussianUnitVarDistribution(BaseDistribution):
    """Gaussian latent distribution with fixed unit variance.

    Parameters:
        n_latent (int): Dimentionality of the latent distribution
        ctx (mxnet.context.Context): Mxnet computational context (cpu or gpu[id])
        dr (float): Dropout value for dropout applied post sample. optional (default = 0.2)
    """
    def __init__(self, n_latent, ctx=mx.cpu(), dr=0.2, var=1.0):
        super(GaussianUnitVarDistribution, self).__init__(n_latent, ctx)
        self.variance = mx.nd.array([var], ctx=ctx)
        self.log_variance = mx.nd.log(self.variance)
        with self.name_scope():
            self.post_sample_dr_o = gluon.nn.Dropout(dr)

    def _get_kl_term(self, mu):
        return -0.5 * torch.sum(1.0 + self.log_variance - mu*mu - self.variance, axis=1)

    def hybrid_forward(self, data, batch_size):
        """Generate a sample according to the unit variance Gaussian given the encoder outputs
        """
        mu = self.mu_encoder(data)
        mu_bn = self.mu_bn(mu)
        z = self._get_gaussian_sample(mu_bn, self.log_variance, batch_size)
        KL = self._get_kl_term(mu_bn)
        return self.post_sample_dr_o(z), KL


class LogisticGaussianDistribution(BaseDistribution):
    """Logistic normal/Gaussian latent distribution with specified prior

    Parameters:
        n_latent (int): Dimentionality of the latent distribution
        ctx (mxnet.context.Context): Mxnet computational context (cpu or gpu[id])
        dr (float): Dropout value for dropout applied post sample. optional (default = 0.2)
        alpha (float): Value the determines prior variance as 1/alpha - (2/n_latent) + 1/(n_latent^2)
    """
    def __init__(self, enc_size, n_latent, ctx=mx.cpu(), dr=0.1, alpha=1.0):
        super(LogisticGaussianDistribution, self).__init__(enc_size, n_latent, ctx)
        self.alpha = alpha

        prior_var = 1 / self.alpha - (2.0 / n_latent) + 1 / (self.n_latent * self.n_latent)
        self.prior_var = mx.nd.array([prior_var], ctx=ctx)
        self.prior_logvar = mx.nd.array([math.log(prior_var)], ctx=ctx)

        self.lv_encoder = nn.Linear(enc_size, n_latent)
        self.lv_bn = nn.BatchNorm(n_latent, momentum = 0.8, epsilon=0.001)
        self.post_sample_dr_o = nn.Dropout(dr)

        #self.lv_bn.collect_params().setattr('grad_req', 'null')        
            

    def _get_kl_term(self, mu, lv):
        posterior_var = torch.exp(lv)
        delta = mu
        dt = torch.div(delta * delta, self.prior_var)
        v_div = torch.div(posterior_var, self.prior_var)
        lv_div = self.prior_logvar - lv
        return 0.5 * (torch.sum((v_div + dt + lv_div), 1) - self.n_latent)

    def hybrid_forward(self, data, batch_size):
        """Generate a sample according to the logistic Gaussian latent distribution given the encoder outputs
        """
        mu = self.mu_encoder(data)
        mu_bn = self.mu_bn(mu)        
        lv = self.lv_encoder(data)
        lv_bn = self.lv_bn(lv)
        z_p = self._get_gaussian_sample(mu_bn, lv_bn, batch_size)
        KL = self._get_kl_term(mu, lv)
        z = self.post_sample_dr_o(z_p)
        return self.softmax(z), KL
    

class HyperSphericalDistribution(BaseDistribution):
    """Hyperspherical (von Mises-Fischer) latent distribution

    Parameters:
        n_latent (int): Dimentionality of the latent distribution
        kappa (float): Concentration parameter for vMF distributioin (default = 100.0)
        dr (float): Dropout value for dropout applied post sample. optional (default = 0.1)
        ctx (:class:`mxnet.context.Context`): Mxnet computational context (cpu or gpu[id])
    """
    def __init__(self, enc_size, n_latent, kappa=100.0, dr=0.1, device='cpu'):
        super(HyperSphericalDistribution, self).__init__(enc_size, n_latent, device)
        self.ctx = ctx
        self.kappa = kappa
        self.kld_v = float(HyperSphericalDistribution._vmf_kld(self.kappa, self.n_latent))
        self.dim = n_latent - 1
        self.b = self.dim / (np.sqrt(4. * kappa ** 2 + self.dim ** 2) + 2 * kappa)  # b= 1/(sqrt(4.* kdiv**2 + 1) + 2 * kdiv)
        self.x = (1. - self.b) / (1. + self.b)
        self.c = self.kappa * self.x + self.dim * np.log(1 - self.x ** 2)  # dim * (kdiv *x + np.log(1-x**2))
        aa = self.dim / 2.0
        self.approx_var = np.sqrt(aa * aa / ( (4 * aa * aa)  * (2 * aa + 1) ))
        self.num_samples = 100000
        self.w_samples = self._pregenerate_samples(num_samples=self.num_samples)

        self.kld_const = self.params.get('kld_const', shape=(1,), init=mx.init.Constant([self.kld_v]), differentiable=False)
        self.vmf_samples = self.params.get('vmf_samples', shape=(self.num_samples,), grad_req='null',
                                               init = mx.init.Constant([self.kld_v]), differentiable=False)
        self.post_sample_dr_o = nn.Dropout(dr)            
        self.been_initialized = False

    def post_init(self, ctx):
        """Method to post initialize the distribution with precomputed set of samples
        """
        self.vmf_samples.set_data(self.w_samples.as_in_context(ctx))
        self.been_initialized = True

    def hybrid_forward(self, data, batch_size, kld_const, vmf_samples):
        """Generate a sample according to the vFM latent distribution given the encoder outputs
        """
        if not self.been_initialized:
            raise Exception("Hyperspherical distribution needs to be initialized after other layers by calling the 'post_init' method")
        mu = self.mu_encoder(data)
        mu_bn = self.mu_bn(mu)
        kld = kld_const.expand(batch_size)
        z_p = self._get_hypersphere_sample(mu_bn, batch_size, vmf_samples)
        z = z_p # self.post_sample_dr_o(z_p)
        z_r = self.softmax(z)
        return z_r, kld

    def _pregenerate_samples(self, num_samples=100000):
        dim = self.n_latent
        kappa = self.kappa
        dim = dim - 1
        b = self.b
        dim = self.dim
        x = self.x
        c = self.c
        mask = mx.nd.ones(num_samples, ctx=self.ctx)
        zeros = mx.nd.zeros(num_samples, ctx=self.ctx)
        w_f = mx.nd.zeros(num_samples, ctx=self.ctx)
        zz = mx.nd.zeros(1, ctx=self.ctx)
        while mx.nd.sum(mask) > 0.0:
            z = mx.nd.clip(mx.nd.random.normal(0.5, self.approx_var, num_samples, ctx=self.ctx), 1e-6, 1.0 - 1e-6)
            w = (1. - (1. + b) * z) / (1. - (1. - b) * z)
            u = mx.nd.random.uniform(0, 1, num_samples, ctx=self.ctx)
            accept = kappa * w + dim * mx.nd.log(1. - x * w) - c >= mx.nd.log(u)
            reject = 1 - accept
            mask = mx.nd.where(accept, zeros, mask)  # if reject = 1 then return mask as is, otherwise turn it off 
            w_f = mx.nd.where(mask, w_f, w)  # if mask is 1, then don't use w and leave as unset
        return w_f
    
    def _get_hypersphere_sample(self, F, mu, batch_size, vmf_samples):
        sw = self._get_weight_from_cache(F, batch_size, vmf_samples)
        #sw = self._get_weight_batch(F, batch_size)
        sw = sw.unsqueeze(1)
        sw_v = sw.expand(batch_size, self.n_latent)
        vv = self._get_orthonormal_batch(mu, batch_size)
        sc11 = torch.ones((batch_size, self.n_latent), device=self.device)
        sc22 = sw_v ** 2.0
        sc_factor = F.sqrt(sc11 - sc22)
        orth_term = vv * sc_factor
        mu_scaled = mu * sw_v
        return orth_term + mu_scaled    

    @staticmethod
    def _vmf_kld(k, d):
        return np.array([(k * ((sp.iv(d / 2.0 + 1.0, k) + sp.iv(d / 2.0, k) * d / (2.0 * k)) / sp.iv(d / 2.0, k) - d / (2.0 * k))
                          + d * np.log(k) / 2.0 - np.log(sp.iv(d / 2.0, k))
                          - sp.loggamma(d / 2 + 1) - d * np.log(2) / 2).real])

    def _get_weight_from_cache(self, F, batch_size, vmf_samples):
        to_select = F.random.randint(low=0, high=self.num_samples, shape=(batch_size,))
        return F.take(vmf_samples, to_select)

    def _get_weight_batch(self, F, batch_size):
        dim = self.n_latent
        kappa = self.kappa
        dim = dim - 1
        b = self.b
        dim = self.dim
        x = self.x
        c = self.c
        mask = torch.ones(batch_size, ctx=self.device)
        zeros = torch.zeros(batch_size, ctx=self.device)
        w_f = torch.zeros(batch_size, ctx=self.device)
        zz = torch.zeros(1, ctx=self.device)
        #while F.broadcast_greater(F.sum(mask), zz):
        while (torch.sum(mask) > zz)
            z = Normal(torch.full((batch_size,), 0.5),
                       torch.full((batch_size,), self.approx_var),
                       device=self.device).sample().clamp(0.000001, 0.99999)
            w = (1. - (1. + b) * z) / (1. - (1. - b) * z)
            u = Uniform(torch.zeros((batch_size,)), torch.ones((batch_size,)), device=self.device).sample()
            accept = kappa * w + dim * torch.log(1. - x * w) - c >= torch.log(u)
            reject = 1 - accept
            mask = torch.where(accept, zeros, mask)  # if reject = 1 then return mask as is, otherwise turn it off 
            w_f = torch.where(mask, w_f, w)  # if mask is 1, then don't use w and leave as unset 
        return w

    def _get_weight_batch_old(self, batch_size):
        batch_sample = torch.zeros((batch_size,), device=self.device)
        for i in range(batch_size):
            batch_sample[i] = self._get_single_weight()
        return batch_sample

    def _get_single_weight(self):
        dim = self.n_latent
        kappa = self.kappa
        dim = dim - 1
        b = self.b
        dim = self.dim
        x = self.x
        c = self.c
        #b = dim / (np.sqrt(4. * kappa ** 2 + dim ** 2) + 2 * kappa)  # b= 1/(sqrt(4.* kdiv**2 + 1) + 2 * kdiv)
        #x = (1. - b) / (1. + b)
        #c = kappa * x + dim * np.log(1 - x ** 2)  # dim * (kdiv *x + np.log(1-x**2))

        while True:
            #z = np.random.beta(dim / 2., dim / 2.)  # concentrates towards 0.5 as d-> inf
            z = min(1.0, max(0.0,np.random.normal(0.5, self.approx_var))) ## approximation with normal for efficiency
            w = (1. - (1. + b) * z) / (1. - (1. - b) * z)
            u = np.random.uniform(low=0, high=1)
            if kappa * w + dim * np.log(1. - x * w) - c >= np.log(u):  # thresh is dim *(kdiv * (w-x) + log(1-x*w) -log(1-x**2))
                return w

    def _get_orthonormal_batch(self, mu, batch_size):
        mu_1       = mu.unsqueeze(1)
        rv         = Normal(torch.zeros(batch_size, self.n_latent, 1),
                            torch.ones(batch_size, self.n_latent, 1),
                            device=self.device)
        rescaled_1 = torch.bmm(mu_1, rv).squeeze(2)
        rescaled   = rescaled_1.expand(batch_size, self.n_latent)
        proj_mu_v  = mu * rescaled        # shape =  (batch_size, n_latent)
        o_vec      = rv.squeeze() - proj_mu_v
        o_norm     = torch.norm(o_vec, dim=1, keepdim=True)
        return     torch.div(o_vec, o_norm)
    
