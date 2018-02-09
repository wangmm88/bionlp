#!/usr/bin/env python
# -*- coding=utf-8 -*-
###########################################################################
# Copyright (C) 2013-2016 by Caspar. All rights reserved.
# File Name: w2v.py
# Author: Shankai Yan
# E-mail: sk.yan@my.cityu.edu.hk
# Created Time: 2017-10-27 15:19:22
###########################################################################
#

import os, sys, time, psutil

import spacy, pysolr
from gensim.models import Word2Vec

from ..util import fs, io, func, njobs


if sys.platform.startswith('win32'):
	DATA_PATH = 'D:\\data\\bionlp\\wordvec'
elif sys.platform.startswith('linux2'):
	DATA_PATH = os.path.join(os.path.expanduser('~'), 'data', 'bionlp', 'wordvec')
MAX_CONN = 16
MAX_TRIAL = None
SC=';;'		


class SolrIterable(object):
	def __init__(self, endpoint, fields=None, query='*:*', offset=0, interval=20, timeout=10, analyzer=None, n_jobs=-1):
		self.endpoint = endpoint
		self.fields = fields
		self.query = query
		self.offset = offset
		self.cutoff = offset
		self.done = False
		self.interval = interval
		self.timeout = timeout
		self.analyzer = analyzer
		self.n_jobs = n_jobs if n_jobs > 0 else psutil.cpu_count()
	def __iter__(self):
		solr = pysolr.Solr(self.endpoint, timeout=self.timeout)
		num_doc = solr.search(self.query, rows=0).hits
		kwargs = dict(rows=self.interval) if self.fields is None else dict(rows=self.interval, fl=self.fields)
		# Create a generator
		def _doc_s():
			for offset in range(self.offset, num_doc, self.interval):
				for r in solr.search(self.query, start=offset, **kwargs):
					yield r
					del r
		def _doc():
			pool = None
			for offset in range(self.offset, num_doc, self.interval):
				sub_interval = self.interval / self.n_jobs
				sub_kwargs = kwargs.copy()
				sub_kwargs['rows'] = sub_interval
				n_jobs = self.n_jobs
				trial = 0 if MAX_TRIAL is None else MAX_TRIAL
				while (MAX_TRIAL is None or trial > 0):
					try:
						docs, pool = njobs.run_pool(_target, n_jobs=min(MAX_CONN, n_jobs), pool=pool, ret_pool=True, dist_param=['start'], endpoint=self.endpoint, timeout=self.timeout, query=self.query, start=range(offset, offset+self.interval, sub_interval), **sub_kwargs)
						for doc in func.flatten_list(docs):
							yield doc
							del doc
						break
					except Exception as e:
						print e
						njobs.run_pool(_target, pool=pool, ret_pool=False)
						pool = None
						n_jobs = max(1, n_jobs / 2)
						time.sleep(20)
					trial -= 1
				else:
					self.cutoff = offset
					break
			else:
				self.cutoff = num_doc
				self.done = True
			njobs.run_pool(_target, pool=pool, ret_pool=False)
		if self.analyzer is None:
			# The iterator generate each document by default
			for doc in _doc():
				yield doc
		else:
			# The analyzer will change the behavior of the iterator
			for x in self.analyzer(_doc()):
				yield x


def _target(endpoint='', timeout=10, query='*:*', start=0, **kw_args):
	trial = 0 if MAX_TRIAL is None else MAX_TRIAL
	while (MAX_TRIAL is None or trial > 0):
		try:
			solr = pysolr.Solr(endpoint, timeout=timeout)
			res = list(solr.search(query, start=start, **kw_args))
			del solr
			break
		except Exception as e:
			print e
			del solr
			time.sleep(10)
		trial -= 1
	else:
		raise RuntimeError('Cannot connect to the Solr service!')
	return res

				
def _spacy_tokenizer(field, batch_size=1000, n_jobs=-1):
	import spacy
	spacy_nlp = spacy.load('en')
	def _tokenizer(docs):
		# Modify the content of each document of the generator
		txt_docs = (doc[field] for doc in docs)
		# Put the text stream into spacy
		for doc in spacy_nlp.pipe(txt_docs, tag=False, parse=True, entity=False, batch_size=batch_size, n_threads=n_jobs if n_jobs > 0 else psutil.cpu_count()):
			# Change the iterator from document level into sentence level
			for sent in doc.sents:
				yield [w.text for w in sent]
			del doc
	return _tokenizer


def pubmed_w2v_solr(endpoint, query='*:*', interval=20, timeout=10, parse_batch=1000, size=100, window=5, min_count=5, cache_path=None, n_jobs=-1):
	mdl_name = os.path.basename(endpoint.rstrip('/'))
	mdl_fname = '%s.mdl' % mdl_name
	if not (cache_path and os.path.isdir(cache_path)):
		cache_path = '.cache'
		fs.mkdir(cache_path)
	cache_fpath, mdl_cache_fpath = os.path.join(cache_path, 'pubmed_w2v.pkl'), os.path.join(cache_path, mdl_fname)
	if (os.path.exists(cache_fpath) and os.path.exists(mdl_cache_fpath)):
		cache = io.read_obj(cache_fpath)
		model = Word2Vec.load(mdl_cache_fpath)
		offset, vocab_built = cache['offset'], cache['vocab_built']
	else:
		model = Word2Vec(None, size=size, window=window, min_count=min_count, workers=n_jobs if n_jobs > 0 else psutil.cpu_count())
		offset, vocab_built = 0, False
	solr_iter = SolrIterable(endpoint, fields=['pmid','abstractText'], query=query, offset=offset, interval=interval, timeout=timeout, analyzer=_spacy_tokenizer(field='abstractText', batch_size=parse_batch, n_jobs=n_jobs), n_jobs=n_jobs)
	if (vocab_built):
		model.train(solr_iter)
	else:
		model.build_vocab(solr_iter, update=offset>0)
		if (solr_iter.done):
			vocab_built = True
			solr_iter = SolrIterable(endpoint, fields=['pmid','abstractText'], query=query, offset=0, interval=interval, timeout=timeout, analyzer=_spacy_tokenizer(field='abstractText', batch_size=parse_batch, n_jobs=n_jobs), n_jobs=n_jobs)
			model.train(solr_iter)

	if (vocab_built and solr_iter.done):
		model.save(mdl_fname)
		print 'Finish training word vector for pubmed!'
	else:
		io.write_obj(dict(offset=solr_iter.cutoff, vocab_built=vocab_built), cache_fpath)
		model.save(mdl_cache_fpath)
		print 'Training is interrupted, model cache is saved in %s' % mdl_cache_fpath