# coding = utf-8
"""
This module implements the reading comprehension models based on:
1. the BiDAF algorithm described in https://arxiv.org/abs/1611.01603
2. the Match-LSTM algorithm described in https://openreview.net/pdf?id=B1-q5Pqxl
Note that we use Pointer Network for the decoding stage of both models.
"""

import json
import logging
import os
import time

import numpy as np
import tensorflow as tf
from tqdm import tqdm

from layers.basic_rnn import rnn
from layers.match_layer import AttentionFlowMatchLayer
from layers.match_layer import MatchLSTMLayer
from layers.pointer_net import PointerNetDecoder
from utils.bleu import Bleu
from utils.rouge import RougeL


class RCModel(object):
	"""
	Implements the main reading comprehension model.
	"""

	def __init__(self, char_vocab, token_vocab, flag_vocab, elmo_vocab, cfg):

		# logging
		self.logger = logging.getLogger("Military AI")

		# basic config
		self.algo = cfg.algo
		# self.suffix = cfg.suffix
		self.hidden_size = cfg.hidden_size
		self.optim_type = cfg.optim
		self.learning_rate = cfg.learning_rate
		self.lr_decay = cfg.lr_decay
		self.loss_type = cfg.loss_type
		self.weight_decay = cfg.weight_decay
		self.use_dropout = cfg.dropout_keep_prob < 1
		self.use_char_emb = cfg.use_char_emb
		self.qtype_count = cfg.qtype_count

		self.ans_max_token_len = cfg.ans_max_token_len
		self.ans_len_inter_size = cfg.ans_len_inter_size
		self.ans_inter_num = self.ans_max_token_len // self.ans_len_inter_size + 1
		self.ans_cut_prob_thresh = cfg.ans_cut_prob_thresh
		self.ans_cut_range = cfg.ans_cut_range
		# the vocab
		self.char_vocab = char_vocab
		self.token_vocab = token_vocab
		self.flag_vocab = flag_vocab
		self.elmo_vocab = elmo_vocab

		# session info
		sess_config = tf.ConfigProto()
		sess_config.gpu_options.allow_growth = True
		self.sess = tf.Session(config=sess_config)

		self._build_graph()

		# save info
		self.saver = tf.train.Saver()

		# initialize the model

		self.sess.run(tf.global_variables_initializer())
		self.logger.info('Model Initialized')

	def _build_graph(self):
		"""
		Builds the computation graph with Tensorflow
		"""
		start_t = time.time()
		self._setup_placeholders()
		self._embed()
		self._encode()
		self._match()
		self._hand_feature()
		self._self_att()
		self._fuse()
		self._pred_ans_inter()
		self._decode()
		self._compute_loss()
		self._create_train_op()
		self.logger.info('Time to build graph: {} s'.format(time.time() - start_t))
		param_num = sum([np.prod(self.sess.run(tf.shape(v))) for v in self.all_params])
		self.logger.info('There are {} parameters in the model'.format(param_num))

	def _setup_placeholders(self):
		"""
		Placeholders
		"""
		self.p_t = tf.placeholder(tf.int32, [None, None])
		self.q_t = tf.placeholder(tf.int32, [None, None])
		self.p_f = tf.placeholder(tf.int32, [None, None])
		self.q_f = tf.placeholder(tf.int32, [None, None])
		self.p_e = tf.placeholder(tf.int32, [None, None])
		self.q_e = tf.placeholder(tf.int32, [None, None])
		self.p_c = tf.placeholder(tf.int32, [None, None, None])
		self.q_c = tf.placeholder(tf.int32, [None, None, None])
		self.p_t_length = tf.placeholder(tf.int32, [None])
		self.q_t_length = tf.placeholder(tf.int32, [None])
		self.p_c_length = tf.placeholder(tf.int32, [None])
		self.q_c_length = tf.placeholder(tf.int32, [None])
		self.start_label = tf.placeholder(tf.int32, [None])
		self.end_label = tf.placeholder(tf.int32, [None])
		# delta stuff
		self.delta_starts = tf.placeholder(tf.int32, [None])
		self.delta_ends = tf.placeholder(tf.int32, [None])
		self.delta_span_idxs = tf.placeholder(tf.int32, [None])
		self.delta_rouges = tf.placeholder(tf.float32, [None])

		self.ans_inter = tf.placeholder(tf.int32, [None])
		self.wiqB = tf.placeholder(tf.float32, [None, None, 1])
		self.qtype_vec = tf.placeholder(tf.float32, [None, self.qtype_count])

		self.p_pad_len = tf.placeholder(tf.int32)
		self.q_pad_len = tf.placeholder(tf.int32)
		self.p_CL = tf.placeholder(tf.int32)
		self.q_CL = tf.placeholder(tf.int32)

		self.dropout_keep_prob = tf.placeholder(tf.float32)
		self.dropout = tf.placeholder_with_default(0.0, (), name="dropout")

	def _embed(self):
		"""
		The embedding layer, question and passage share embeddings
		"""
		with tf.variable_scope('token_embedding'):
			with tf.device('/cpu:0'):
				self.token_embeddings = tf.get_variable(
					'token_embedding',
					shape=(self.token_vocab.size(), self.token_vocab.embed_dim),
					initializer=tf.constant_initializer(self.token_vocab.embeddings),
					trainable=False
				)
			self.p_t_emb = tf.nn.embedding_lookup(self.token_embeddings, self.p_t)
			self.q_t_emb = tf.nn.embedding_lookup(self.token_embeddings, self.q_t)

		with tf.variable_scope('flag_embedding'):
			with tf.device('/cpu:0'):
				self.flag_embeddings = tf.get_variable(
					'flag_embedding',
					shape=(self.flag_vocab.size(), self.flag_vocab.embed_dim),
					initializer=tf.constant_initializer(self.flag_vocab.embeddings),
					trainable=True
				)
			p_f_emb = tf.nn.embedding_lookup(self.flag_embeddings, self.p_f)
			q_f_emb = tf.nn.embedding_lookup(self.flag_embeddings, self.q_f)

		with tf.variable_scope('elmo_embedding'):
			with tf.device('/cpu:0'):
				self.elmo_embeddings = tf.get_variable(
					'elmo_embedding',
					shape=(self.elmo_vocab.size(), self.elmo_vocab.embed_dim),
					initializer=tf.constant_initializer(self.elmo_vocab.embeddings),
					trainable=False
				)
			p_e_emb = tf.nn.embedding_lookup(self.elmo_embeddings, self.p_e)
			q_e_emb = tf.nn.embedding_lookup(self.elmo_embeddings, self.q_e)

		self.p_t_emb = tf.concat([self.p_t_emb, p_f_emb, p_e_emb], axis=-1)
		self.q_t_emb = tf.concat([self.q_t_emb, q_f_emb, q_e_emb], axis=-1)
		# if self.use_dropout:
		#     self.p_t_emb = tf.nn.dropout(self.p_t_emb, self.dropout_keep_prob)
		#     self.q_t_emb = tf.nn.dropout(self.q_t_emb, self.dropout_keep_prob)

		if self.use_char_emb:
			with tf.variable_scope('char_embedding'):
				with tf.device('/cpu:0'):
					self.char_embeddings = tf.get_variable(
						'char_embedding',
						shape=(self.char_vocab.size(), self.char_vocab.embed_dim),
						initializer=tf.constant_initializer(self.char_vocab.embeddings),
						trainable=False
					)

				self.p_c_emb = tf.nn.embedding_lookup(self.char_embeddings, self.p_c)
				self.q_c_emb = tf.nn.embedding_lookup(self.char_embeddings, self.q_c)
				batch_size = tf.shape(self.start_label)[0]
				self.p_c_emb = tf.reshape(self.p_c_emb,
										  [batch_size * self.p_pad_len, self.p_CL, self.char_vocab.embed_dim])
				self.q_c_emb = tf.reshape(self.q_c_emb,
										  [batch_size * self.q_pad_len, self.q_CL, self.char_vocab.embed_dim])

	# if self.use_dropout:
	#     self.p_c_emb = tf.nn.dropout(self.p_c_emb, self.dropout_keep_prob)
	#     self.q_c_emb = tf.nn.dropout(self.q_c_emb, self.dropout_keep_prob)

	def _encode(self):
		"""
		Employs two Bi-LSTMs to encode passage and question separately
		"""
		with tf.variable_scope('encode'):
			batch_size = tf.shape(self.start_label)[0]
			with tf.variable_scope('passage_encoding'):
				with tf.variable_scope('token_level'):
					sep_p_t_encodes, _ = rnn('bi-lstm', self.p_t_emb, self.p_t_length, self.hidden_size)
				if self.use_char_emb:
					with tf.variable_scope('char_level'):
						_, sep_p_c_encodes = rnn('bi-lstm', self.p_c_emb, self.p_c_length, self.hidden_size)

						sep_p_c_encodes = tf.reshape(sep_p_c_encodes, [batch_size, self.p_pad_len,
																	   self.hidden_size * 2])
					self.sep_p_encodes = tf.concat([sep_p_t_encodes, sep_p_c_encodes], axis=-1)
				else:
					self.sep_p_encodes = sep_p_t_encodes
			with tf.variable_scope('question_encoding'):
				with tf.variable_scope('token_level'):
					sep_q_t_encodes, _ = rnn('bi-lstm', self.q_t_emb, self.q_t_length, self.hidden_size)
				if self.use_char_emb:
					with tf.variable_scope('char_level'):
						_, sep_q_c_encodes = rnn('bi-lstm', self.q_c_emb, self.q_c_length, self.hidden_size)
						sep_q_c_encodes = tf.reshape(sep_q_c_encodes, [batch_size, self.q_pad_len,
																	   self.hidden_size * 2])
					self.sep_q_encodes = tf.concat([sep_q_t_encodes, sep_q_c_encodes], axis=-1)
				else:
					self.sep_q_encodes = sep_q_t_encodes
			if self.use_dropout:
				self.sep_p_encodes = tf.nn.dropout(self.sep_p_encodes, self.dropout_keep_prob)
				self.sep_q_encodes = tf.nn.dropout(self.sep_q_encodes, self.dropout_keep_prob)

	def _match(self):
		"""
		The core of RC model, get the question-aware passage encoding with either BIDAF or MLSTM
		"""
		with tf.variable_scope('match'):
			if self.algo == 'MLSTM':
				match_layer = MatchLSTMLayer(self.hidden_size)
			elif self.algo == 'BIDAF':
				match_layer = AttentionFlowMatchLayer(self.hidden_size)
			else:
				raise NotImplementedError('The algorithm {} is not implemented.'.format(self.algo))
			self.match_p_encodes, _ = match_layer.match(self.sep_p_encodes, self.sep_q_encodes,
														self.p_t_length, self.q_t_length)

			self.match_p_encodes, _ = rnn('bi-lstm', self.match_p_encodes, self.p_t_length,
										  self.hidden_size, layer_num=1)

			if self.use_dropout:
				self.match_p_encodes = tf.nn.dropout(self.match_p_encodes, self.dropout_keep_prob)

	def _hand_feature(self):
		"""
		Concats hand features
		"""
		with tf.variable_scope('hand_feature'):
			batch_size = tf.shape(self.start_label)[0]
			self.wiqB = tf.reshape(self.wiqB, [batch_size, self.p_pad_len, 1])
			self.match_p_encodes = tf.concat([self.match_p_encodes, self.wiqB], axis=-1)

	def _self_att(self):
		"""
		Self attention layer
		"""
		with tf.variable_scope('self_att'):
			if self.algo == 'MLSTM':
				self_att_layer = MatchLSTMLayer(self.hidden_size)
			elif self.algo == 'BIDAF':
				self_att_layer = AttentionFlowMatchLayer(self.hidden_size)
			else:
				raise NotImplementedError('The algorithm {} is not implemented.'.format(self.algo))
			self.self_att_p_encodes, _ = self_att_layer.match(self.match_p_encodes, self.match_p_encodes,
															  self.p_t_length, self.p_t_length)
			if self.use_dropout:
				self.self_att_p_encodes = tf.nn.dropout(self.self_att_p_encodes, self.dropout_keep_prob)

	def _fuse(self):
		"""
		Employs Bi-LSTM again to fuse the context information after match layer
		"""
		with tf.variable_scope('fusion'):
			self.fuse_p_encodes, _ = rnn('bi-lstm', self.self_att_p_encodes, self.p_t_length,
										 self.hidden_size, layer_num=1)

			if self.use_dropout:
				self.fuse_p_encodes = tf.nn.dropout(self.fuse_p_encodes, self.dropout_keep_prob)

	def _pred_ans_inter(self):
		with tf.variable_scope("ans_inter"):
			last_output = tf.reduce_max(self.fuse_p_encodes, axis=1)

			softmax_w = tf.get_variable("softmax_w",
										[last_output.get_shape().as_list()[-1], self.ans_inter_num],
										dtype=tf.float32)
			softmax_b = tf.get_variable("softmax_b", [self.ans_inter_num], dtype=tf.float32)
			self.ans_inter_logits = tf.matmul(last_output, softmax_w) + softmax_b
			self.ans_inter_prob = tf.nn.softmax(self.ans_inter_logits, axis=-1)

	def _decode(self):
		"""
		Employs Pointer Network to get the the probs of each position
		to be the start or end of the predicted answer.
		Note that we concat the fuse_p_encodes for the passages in the same document.
		And since the encodes of queries in the same document is same, we select the first one.
		"""

		with tf.variable_scope('decode'):
			decoder = PointerNetDecoder(self.hidden_size)
			self.start_probs, self.end_probs = decoder.decode(self.fuse_p_encodes, self.sep_q_encodes)
			self.prob_matirx = tf.matmul(tf.expand_dims(self.start_probs, axis=2),
										 tf.expand_dims(self.end_probs, axis=1))

	# self.band_prob_matirx = tf.matrix_band_part(self.prob_matirx, 0, -1)
	# self.pred_starts = tf.argmax(tf.reduce_max(self.band_prob_matirx, axis=2), axis=-1)
	# self.pred_ends = tf.argmax(tf.reduce_max(self.band_prob_matirx, axis=1), axis=-1)

	# out_ans_len = self.pred_ends - self.pred_starts + 1
	# pred_ans_len = (self.pred_ans_inter + 1) * self.ans_len_inter_size
	# ans_len_thresh = tf.to_int64(tf.fill(tf.shape(self.pred_ans_inter), self.ans_max_token_len + 1))
	#
	# out_ans_len_mask = tf.greater_equal(out_ans_len, ans_len_thresh)
	# pred_ans_len_mask = tf.less(pred_ans_len, ans_len_thresh)
	#
	# final_ans_len = tf.where(out_ans_len_mask, out_ans_len, pred_ans_len)
	# final_ans_len = tf.where(pred_ans_len_mask, pred_ans_len, final_ans_len)
	#
	# self.pred_ends = self.pred_starts + final_ans_len - 1

	def _compute_loss(self):
		"""
		The loss function
		"""

		def sparse_nll_loss(probs, labels, epsilon=1e-9, scope=None):
			"""
			negative log likelyhood loss
			"""
			with tf.name_scope(scope, "log_loss"):
				labels = tf.one_hot(labels, tf.shape(probs)[1], axis=1)
				losses = - tf.reduce_sum(labels * tf.log(probs + epsilon), 1)
			return losses

		with tf.variable_scope("pointer_loss"):
			self.start_loss = sparse_nll_loss(probs=self.start_probs, labels=self.start_label)
			self.end_loss = sparse_nll_loss(probs=self.end_probs, labels=self.end_label)
			self.all_params = tf.trainable_variables()
			self.pointer_loss = tf.reduce_mean(tf.add(self.start_loss, self.end_loss))

		with tf.variable_scope("type_loss"):
			last_output = self.sep_q_encodes[:, -1, :]
			softmax_w = tf.get_variable("softmax_w",
										[last_output.get_shape().as_list()[-1], self.qtype_count],
										dtype=tf.float32)
			softmax_b = tf.get_variable("softmax_b", [self.qtype_count], dtype=tf.float32)
			type_logits = tf.matmul(last_output, softmax_w) + softmax_b

			self.type_loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits_v2(
				labels=self.qtype_vec, logits=type_logits))

		with tf.variable_scope('ans_inter_loss'):
			inter_vec = tf.one_hot(self.ans_inter, self.ans_inter_num, axis=1)
			self.ans_inter_loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits_v2(
				labels=inter_vec, logits=self.ans_inter_logits))

		with tf.variable_scope("mrl"):
			batch_size = tf.shape(self.start_label)[0]
			out_matrix = tf.reshape(self.prob_matirx, [batch_size, self.p_pad_len ** 2])
			# hard
			delta_hard_pos = self.start_label * self.p_pad_len + self.end_label
			batch_idx_hard = tf.range(0, batch_size)
			indices_hard = tf.to_int64(tf.stack([batch_idx_hard, delta_hard_pos], axis=1))
			delta_hard = 1.0 - tf.sparse_to_dense(indices_hard,
												  tf.to_int64([batch_size, self.p_pad_len ** 2]),
												  1.0, 0.0)

			# soft
			delta_soft_pos = self.delta_starts * self.p_pad_len + self.delta_ends
			indices_soft = tf.to_int64(tf.stack([self.delta_span_idxs, delta_soft_pos], axis=1))
			delta_soft = 1.0 - tf.sparse_to_dense(indices_soft,
												  tf.to_int64([batch_size, self.p_pad_len ** 2]),
												  self.delta_rouges, 0.0)

			delta_mix = tf.reduce_min(tf.stack([delta_hard, delta_soft], axis=2), axis=-1)

			self.mrl_hard = tf.reduce_mean(tf.reduce_sum(delta_hard * out_matrix, axis=-1))
			self.mrl_soft = tf.reduce_mean(tf.reduce_sum(delta_soft * out_matrix, axis=-1))
			self.mrl_mix = tf.reduce_mean(tf.reduce_sum(delta_mix * out_matrix, axis=-1))

		if self.loss_type == 'pointer':
			self.mrl = tf.constant(0, dtype=tf.float32)
			self.loss = self.pointer_loss + 0.1 * self.type_loss + 0.2 * self.ans_inter_loss
		elif self.loss_type == 'mrl_mix':
			self.mrl = self.mrl_mix
			self.loss = self.mrl + 0.1 * self.type_loss + 0.2 * self.ans_inter_loss
		elif self.loss_type == 'mrl_soft':
			self.mrl = self.mrl_soft
			self.loss = self.mrl + 0.1 * self.type_loss + 0.2 * self.ans_inter_loss
		elif self.loss_type == 'mrl_hard':
			self.mrl = self.mrl_hard
			self.loss = self.mrl + 0.1 * self.type_loss + 0.2 * self.ans_inter_loss
		else:
			assert 0 != 0

		if self.weight_decay > 0:
			with tf.variable_scope('l2_loss'):
				l2_loss = tf.add_n([tf.nn.l2_loss(v) for v in self.all_params])
			self.loss += self.weight_decay * l2_loss

	def _create_train_op(self):
		"""
		Selects the training algorithm and creates a train operation with it
		"""
		lr = self.learning_rate
		global_step = tf.train.get_or_create_global_step()
		if self.lr_decay < 1:
			self.decay_learning_rate = tf.train.exponential_decay(
				self.learning_rate,
				global_step,
				300,
				self.lr_decay
			)
			lr = self.decay_learning_rate

		if self.optim_type == 'adagrad':
			self.optimizer = tf.train.AdagradOptimizer(lr)
		elif self.optim_type == 'adam':
			self.optimizer = tf.train.AdamOptimizer(lr)
		elif self.optim_type == 'rprop':
			self.optimizer = tf.train.RMSPropOptimizer(self.learning_rate, self.lr_decay)
		elif self.optim_type == 'adadelta':
			self.optimizer = tf.train.AdadeltaOptimizer(self.learning_rate, self.lr_decay)
		elif self.optim_type == 'sgd':
			self.optimizer = tf.train.GradientDescentOptimizer(lr)
		else:
			raise NotImplementedError('Unsupported optimizer: {}'.format(self.optim_type))
		self.train_op = self.optimizer.minimize(self.loss, global_step=global_step)

	def _train_epoch(self, train_batches, dropout_keep_prob,
					 data, max_rouge_l, n_ep, save_dir, save_prefix):
		"""
		Trains the model for a single epoch.
		Args:
			train_batches: iterable batch data for training
			dropout_keep_prob: float value indicating dropout keep probability
		"""
		total_num, total_mrl, total_pointer_loss, total_ans_acc = 0, 0, 0, 0
		log_every_n_batch, eval_every_n_batch, n_batch_mrl, n_batch_pointer_loss, n_batch_ans_acc = 50, 1000, 0, 0, 0
		for bitx, batch in enumerate(train_batches, 1):
			feed_dict = {self.p_t: batch['article_token_ids'],
						 self.q_t: batch['question_token_ids'],
						 self.p_f: batch['article_flag_ids'],
						 self.q_f: batch['question_flag_ids'],
						 self.p_e: batch['article_elmo_ids'],
						 self.q_e: batch['question_elmo_ids'],
						 self.p_pad_len: batch['article_pad_len'],
						 self.q_pad_len: batch['question_pad_len'],
						 self.p_t_length: batch['article_tokens_len'],
						 self.q_t_length: batch['question_tokens_len'],

						 self.start_label: batch['start_id'],
						 self.end_label: batch['end_id'],
						 self.wiqB: batch['wiqB'],
						 self.qtype_vec: batch['qtype_vecs'],
						 # answer inter
						 self.ans_inter: batch['answer_inter'],
						 # delta stuff
						 self.delta_starts: batch['delta_token_starts'],
						 self.delta_ends: batch['delta_token_ends'],
						 self.delta_span_idxs: batch['delta_span_idxs'],
						 self.delta_rouges: batch['delta_rouges'],

						 self.dropout_keep_prob: dropout_keep_prob}
			if self.use_char_emb:
				feed_dict.update(
					{self.p_c: batch['article_char_ids'],
					 self.q_c: batch['question_char_ids'],
					 self.p_c_length: batch['article_c_len'],
					 self.q_c_length: batch['question_c_len'],
					 self.p_CL: batch['article_CL'],
					 self.q_CL: batch['question_CL']
					 })
			# print('article CL:{}\n'
			#       'question_CL:{}\n'
			#       'article pad len:{}\n'
			#       'question pad len:{}'.format(batch['article_CL'], batch['question_CL'],
			#                                    batch['article_pad_len'], batch['question_pad_len']))
			# print(batch['question_char_ids'])
			_, mrl, pointer_loss, type_loss, pred_ans_inter_prob = self.sess.run(
				[self.train_op, self.mrl, self.pointer_loss, self.type_loss, self.ans_inter_prob], feed_dict)
			pred_ans_inter = np.argmax(pred_ans_inter_prob, -1)
			batch_size = len(batch['raw_data'])
			ans_acc = np.sum(batch['answer_inter'] == pred_ans_inter)
			# self.logger.info('pred ans len inter {}'.format(list(pred_ans_inter)))
			# self.logger.info('gt ans len inter {}'.format(batch['answer_inter']))
			total_ans_acc += ans_acc
			total_mrl += mrl * batch_size
			total_pointer_loss += pointer_loss * batch_size
			total_num += batch_size
			n_batch_mrl += mrl
			n_batch_pointer_loss += pointer_loss
			n_batch_ans_acc += ans_acc / batch_size
			if log_every_n_batch > 0 and bitx % log_every_n_batch == 0:
				self.logger.info('Average loss from batch {} to {} is MRL Loss: {}, Pointer Loss: {}'.format(
					bitx - log_every_n_batch + 1, bitx, n_batch_mrl / log_every_n_batch,
					n_batch_pointer_loss / log_every_n_batch))
				self.logger.info('Ans Len Acc is {}'.format(n_batch_ans_acc / log_every_n_batch))
				n_batch_mrl = 0
				n_batch_pointer_loss = 0
				n_batch_ans_acc = 0
			if eval_every_n_batch > 0 and bitx > 0 and bitx % eval_every_n_batch == 0:
				self.logger.info('Evaluating the model after batch {}'.format(bitx))
				if data.dev_set is not None:
					eval_batches = data.gen_mini_batches('dev', batch_size, shuffle=False)
					eval_mrl, eval_pointer_loss, eval_ans_acc, bleu_rouge = self.evaluate(eval_batches)
					self.logger.info('Dev eval mrl {}'.format(eval_mrl))
					self.logger.info('Dev eval pointer loss {}'.format(eval_pointer_loss))
					self.logger.info('Dev eval ans len acc: {}'.format(eval_ans_acc))
					self.logger.info('Dev eval result: {}'.format(bleu_rouge))

					if bleu_rouge['Rouge-L'] > max_rouge_l:
						self.save(save_dir, save_prefix)
						max_rouge_l = bleu_rouge['Rouge-L']
				else:
					self.logger.warning('No dev set is loaded for evaluation in the dataset!')

		return 1.0 * total_mrl / total_num, 1.0 * total_pointer_loss / total_num, 1.0 * total_ans_acc / total_num, max_rouge_l

	def train(self, data, epochs, batch_size, save_dir, save_prefix,
			  dropout_keep_prob=1.0, evaluate=True):
		"""
		Train the model with data
		Args:
			data: the BRCDataset class implemented in dataset.py
			epochs: number of training epochs
			batch_size:
			save_dir: the directory to save the model
			save_prefix: the prefix indicating the model type
			dropout_keep_prob: float value indicating dropout keep probability
			evaluate: whether to evaluate the model on test set after each epoch
		"""
		max_rouge_l = 0
		for epoch in tqdm(range(1, epochs + 1)):
			self.logger.info('Training the model for epoch {}'.format(epoch))
			train_batches = data.gen_mini_batches('train', batch_size, shuffle=True)
			train_mrl, train_pointer_loss, train_ans_acc, max_rouge_l = self._train_epoch(train_batches,
																						  dropout_keep_prob, data,
																						  max_rouge_l, epoch,
																						  save_dir, save_prefix)

			self.logger.info(
				'Average train loss for epoch {} is MRL Loss: {}, Pointer Loss: {}'.format(epoch, train_mrl,
																						   train_pointer_loss))
			self.logger.info('Ans Len Acc is {}'.format(train_ans_acc))

			if evaluate:
				self.logger.info('Evaluating the model after epoch {}'.format(epoch))
				if data.dev_set is not None:
					eval_batches = data.gen_mini_batches('dev', batch_size, shuffle=False)
					eval_mrl, eval_pointer_loss, eval_ans_acc, bleu_rouge = self.evaluate(eval_batches)
					self.logger.info('Dev eval mrl {}'.format(eval_mrl))
					self.logger.info('Dev eval pointer loss {}'.format(eval_pointer_loss))
					self.logger.info('Dev eval ans len acc: {}'.format(eval_ans_acc))
					self.logger.info('Dev eval result: {}'.format(bleu_rouge))

					if bleu_rouge['Rouge-L'] > max_rouge_l:
						self.save(save_dir, save_prefix)
						max_rouge_l = bleu_rouge['Rouge-L']
				else:
					self.logger.warning('No dev set is loaded for evaluation in the dataset!')
			else:
				self.save(save_dir, save_prefix + '_' + str(epoch))

	def evaluate(self, eval_batches, result_dir=None, result_prefix=None, save_full_info=False):
		"""
		Evaluates the model performance on eval_batches and results are saved if specified
		Args:
			eval_batches: iterable batch data
			result_dir: directory to save predicted answers, answers will not be saved if None
			result_prefix: prefix of the file for saving predicted answers,
						   answers will not be saved if None
			save_full_info: if True, the pred_answers will be added to raw sample and saved
		"""
		pred_answers, ref_answers = [], []
		total_mrl, total_pointer_loss, total_num, total_ans_acc = 0, 0, 0, 0
		rl_cut, bleu_cut = RougeL(), Bleu()
		rl_gt_cut, bleu_gt_cut = RougeL(), Bleu()
		rl_org, bleu_org = RougeL(), Bleu()

		ariticle_map = {}
		for b_itx, batch in enumerate(eval_batches):
			feed_dict = {self.p_t: batch['article_token_ids'],
						 self.q_t: batch['question_token_ids'],
						 self.p_f: batch['article_flag_ids'],
						 self.q_f: batch['question_flag_ids'],
						 self.p_e: batch['article_elmo_ids'],
						 self.q_e: batch['question_elmo_ids'],
						 self.p_pad_len: batch['article_pad_len'],
						 self.q_pad_len: batch['question_pad_len'],
						 self.p_t_length: batch['article_tokens_len'],
						 self.q_t_length: batch['question_tokens_len'],

						 self.start_label: batch['start_id'],
						 self.end_label: batch['end_id'],
						 self.wiqB: batch['wiqB'],
						 self.qtype_vec: batch['qtype_vecs'],
						 # answer inter
						 self.ans_inter: batch['answer_inter'],

						 # delta stuff
						 self.delta_starts: batch['delta_token_starts'],
						 self.delta_ends: batch['delta_token_ends'],
						 self.delta_span_idxs: batch['delta_span_idxs'],
						 self.delta_rouges: batch['delta_rouges'],

						 self.dropout_keep_prob: 1.0}

			if self.use_char_emb:
				feed_dict.update(
					{self.p_c: batch['article_char_ids'],
					 self.q_c: batch['question_char_ids'],
					 self.p_c_length: batch['article_c_len'],
					 self.q_c_length: batch['question_c_len'],
					 self.p_CL: batch['article_CL'],
					 self.q_CL: batch['question_CL']
					 })
			prob_matrix, ans_inter_prob, mrl, pointer_loss = self.sess.run([self.prob_matirx,
																			self.ans_inter_prob,
																			self.mrl,
																			self.pointer_loss],
																		   feed_dict)
			ans_inter = np.argmax(ans_inter_prob, -1)
			batch_size = len(batch['raw_data'])
			total_mrl += mrl * batch_size
			total_pointer_loss += pointer_loss * batch_size
			total_num += batch_size
			total_ans_acc += np.sum(ans_inter == batch['answer_inter'])

			for sample, out_matrix, inter_prob in zip(batch['raw_data'], prob_matrix.tolist(), ans_inter_prob.tolist()):
				best_answer_origin, best_answer_cut, best_answer_gt_cut = self.find_best_answer(sample, out_matrix, inter_prob)
				if sample['article_id'] not in ariticle_map:
					ariticle_map[sample['article_id']] = len(ariticle_map)
					pred_answers.append({'article_id': sample['article_id'],
										 'questions': []
										 })
					ref_answers.append({'article_id': sample['article_id'],
										'questions': []
										})

				pred_answers[ariticle_map[sample['article_id']]]['questions'].append(
					{'question_id': sample['question_id'],
					 'answer': best_answer_cut
					 })
				ref_answers[ariticle_map[sample['article_id']]]['questions'].append(
					{'question_id': sample['question_id'],
					 'answer': sample['answer']
					 })

				rl_cut.add_inst(best_answer_cut, sample['answer'])
				bleu_cut.add_inst(best_answer_cut, sample['answer'])

				rl_org.add_inst(best_answer_origin, sample['answer'])
				bleu_org.add_inst(best_answer_origin, sample['answer'])

				rl_gt_cut.add_inst(best_answer_gt_cut, sample['answer'])
				bleu_gt_cut.add_inst(best_answer_gt_cut, sample['answer'])

		# compute the bleu and rouge scores
		rougel_cut = rl_cut.get_score()
		bleu4_cut = bleu_cut.get_score()
		rougel_gt_cut = rl_gt_cut.get_score()
		bleu4_gt_cut = bleu_gt_cut.get_score()
		rougel_org = rl_org.get_score()
		bleu4_org = bleu_org.get_score()
		bleu_rouge = {'Rouge-L[cut]': rougel_cut,
					  'Bleu-4[cut]': bleu4_cut,
					  'Rouge-L[gt_cut]': rougel_gt_cut,
					  'Bleu-4[gt_cut]': bleu4_gt_cut,
					  'Rouge-L[org]': rougel_org,
					  'Bleu-4[org]': bleu4_org
					  }

		if result_dir is not None and result_prefix is not None:
			result_file = os.path.join(result_dir, result_prefix + '.json')
			with open(result_file, 'w') as fout:
				# for pred_answer in pred_answers:
				#     fout.write(json.dumps(pred_answer, ensure_ascii=False) + '\n')
				json.dump(pred_answers, fout, ensure_ascii=False)

			self.logger.info('Saving {} results to {}'.format(result_prefix, result_file))

		# this average loss is invalid on test set, since we don't have true start_id and end_id
		ave_mrl = 1.0 * total_mrl / total_num
		ave_pointer_loss = 1.0 * total_pointer_loss / total_num
		ave_ans_acc = 1.0 * total_ans_acc / total_num
		return ave_mrl, ave_pointer_loss, ave_ans_acc, bleu_rouge

	def find_best_answer(self, sample, prob_matrix, ans_inter_prob, passage_len=None):
		"""
		Finds the best answer with the maximum start_prob * end_prob from a single passage
		"""
		if passage_len is None:
			passage_len = len(sample['article_tokens'])
		else:
			passage_len = min(len(sample['article_tokens']), passage_len)
		pred_ans_inter = int(np.argmax(ans_inter_prob))
		pred_ans_inter_prob = ans_inter_prob[pred_ans_inter]

		# upper
		prob_matrix = np.triu(prob_matrix, 0)

		pred_start_origin = np.argmax(np.max(prob_matrix, axis=1), axis=0)
		pred_end_origin = min(np.argmax(np.max(prob_matrix, axis=0), axis=0), passage_len - 1)

		matrix = prob_matrix
		if len(sample['answer_tokens']) < self.ans_len_inter_size * self.ans_inter_num:
			matrix = np.triu(prob_matrix, k=max(0, len(sample['answer_tokens']) - self.ans_cut_range))
			matrix = np.tril(matrix, k=len(sample['answer_tokens']) + self.ans_cut_range)

		pred_start_gt_cut = np.argmax(np.max(matrix, axis=1), axis=0)
		pred_end_gt_cut = min(np.argmax(np.max(matrix, axis=0), axis=0), passage_len - 1)

		if pred_ans_inter < self.ans_inter_num - 1 and pred_ans_inter_prob >= self.ans_cut_prob_thresh:
			pred_ans_len = self.ans_len_inter_size * (pred_ans_inter + 1)

			prob_matrix = np.triu(prob_matrix, k=max(0, pred_ans_len - self.ans_cut_range))
			prob_matrix = np.tril(prob_matrix, k=pred_ans_len + self.ans_cut_range)

		pred_start_cut = np.argmax(np.max(prob_matrix, axis=1), axis=0)
		pred_end_cut = min(np.argmax(np.max(prob_matrix, axis=0), axis=0), passage_len - 1)

		best_answer_origin = ''.join(sample['article_tokens'][pred_start_origin: pred_end_origin + 1])
		best_answer_cut = ''.join(sample['article_tokens'][pred_start_cut: pred_end_cut + 1])
		best_answer_gt_cut = ''.join(sample['article_tokens'][pred_start_gt_cut: pred_end_gt_cut + 1])
		return best_answer_origin, best_answer_cut, best_answer_gt_cut

	def save(self, model_dir, model_prefix):
		"""
		Saves the model into model_dir with model_prefix as the model indicator
		"""
		self.saver.save(self.sess, os.path.join(model_dir, model_prefix))
		self.logger.info('Model saved in {}, with prefix {}.'.format(model_dir, model_prefix))

	def restore(self, model_dir, model_prefix):
		"""
		Restores the model into model_dir from model_prefix as the model indicator
		"""
		self.saver.restore(self.sess, os.path.join(model_dir, model_prefix))
		self.logger.info('Model restored from {}, with prefix {}'.format(model_dir, model_prefix))
