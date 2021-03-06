# coding = utf-8
import json
import logging
import multiprocessing as mp
import os
import re
from functools import partial
from pyltp import Segmentor, Postagger

import jieba
import jieba.posseg as pseg
import numpy as np
import pandas as pd

from utils.rouge import RougeL


ltp_seg = Segmentor()
ltp_pos = Postagger()


class PreProcessor():


	def __init__(self, cfg):
		self.cfg = cfg
		self.ltp_seg = None
		self.ltp_pos = None
		if cfg.cut_word_method == 'jieba':
			if os.path.isfile(cfg.jieba_big_dict_path):
				jieba.set_dictionary(cfg.jieba_big_dict_path)
			jieba.setLogLevel(logging.INFO)
		elif cfg.cut_word_method == 'pyltp':
			ltp_seg.load(cfg.pyltp_cws_model_path)
			ltp_pos.load(cfg.pyltp_pos_model_path)

	@staticmethod
	def trans_to_df(raw_data_path):
		"""
		transfer json file to dataframe
		:param raw_data_path: path of raw data of json file
		:return: article_df, qa_df
		"""

		with open(raw_data_path, 'r') as file:
			data = json.load(file)
			questions = []
			articles = []

			for dc in data:
				temp = [dc['article_id'], dc['article_type'], dc['article_title'], dc['article_content']]
				articles.append(temp)
				for items in dc['questions']:
					r = [dc['article_id']]
					r = r + list(items.values())
					questions.append(r)

			article_columns = ['article_id', 'article_type', 'article_title', 'article_content']
			if 'answer' in data[0]['questions'][0]:
				question_columns = ['article_id', 'question_id', 'question', 'answer', 'question_type']
			else:
				question_columns = ['article_id', 'question_id', 'question']
			article_df = pd.DataFrame(data=articles, columns=article_columns)
			qa_df = pd.DataFrame(data=questions, columns=question_columns)

		return article_df, qa_df

	@staticmethod
	def clean_text(article_df: pd.DataFrame, qa_df: pd.DataFrame):
		"""
		remove \u3000, swap \r\n with \001, swap \t with ' ',
		change numbers to half written
		:param df:
		:return: cleaned df
		"""

		def clean(row):
			row = re.sub('[\u3000\t]', ' ', row)
			row = re.sub('\s{2,}', '', row)
			row = re.sub('[“”]', '', row)
			row = re.sub('[\r\n]', ' ', row)

			# p_l = re.compile(r'\s+([\u4e00-\u9fa5, ]{1})')
			# p_r = re.compile(r'([\u4e00-\u9fa5, ]{1})\s+')
			# row = p_l.sub('\1', row)
			# row = p_r.sub('\1', row)

			row = re.sub(r'０', '0', row)
			row = re.sub(r'１', '1', row)
			row = re.sub(r'２', '2', row)
			row = re.sub(r'３', '3', row)
			row = re.sub(r'４', '4', row)
			row = re.sub(r'５', '5', row)
			row = re.sub(r'６', '6', row)
			row = re.sub(r'７', '7', row)
			row = re.sub(r'８', '8', row)
			row = re.sub(r'９', '9', row)
			row = re.sub(r'．', '.', row)

			if len(row) > 0 and row[-1] == '。':
				row = row[:-1].strip()
			return row

		def merge(row):
			"""
			merge the article title and content
			:param row:
			:return:
			"""
			row['article'] = row['article_title'] + '。' + row['article_content']
			return row

		article_df['article_title'] = article_df['article_title'].apply(clean)
		article_df['article_content'] = article_df['article_content'].apply(clean)

		article_df = article_df.apply(merge, axis=1)
		article_df.drop(['article_title', 'article_content'], axis=1, inplace=True)

		qa_df['question'] = qa_df['question'].apply(clean)
		if 'answer' in qa_df.columns:
			qa_df['answer'] = qa_df['answer'].apply(clean)
			qa_df['answer'] = qa_df['answer'].apply(str.strip)
			answers = qa_df[qa_df['answer'] != '']['answer'].values
			drop_list = ['。', '，', '：', '！', '？', ' ']
			answers = [answer[:-1].strip() if answer[-1] in drop_list else answer for answer in answers]
			answers = [answer[1:].strip() if answer[0] in drop_list else answer for answer in answers]
			qa_df.loc[qa_df['answer'] != '', 'answer'] = answers
		return article_df, qa_df

	@staticmethod
	def _apply_cut_jieba(df, col):
		def _cut(row):
			"""
			cut the sentences into tokens
			:param row:
			:return:
			"""

			cut_res = pseg.lcut(row, HMM=False)
			new_row = pd.Series()
			new_row['tokens'] = [res.word for res in cut_res]
			new_row['flags'] = [res.flag for res in cut_res]
			return new_row

		sentence_cut = df[col].apply(_cut)
		return sentence_cut


	def _apply_cut_pyltp(self, df, col):

		def _cut(row):
			"""
			cut the sentences into tokens
			:param row:
			:return:
			"""
			if '。' in row:
				cut_res = []
				row = row.split('。')
				for idx, s in enumerate(row):
					s_cut = list(ltp_seg.segment(s))
					cut_res.extend(s_cut)
					if idx != len(row) - 1:
						cut_res.append('。')
			else:
				cut_res = list(ltp_seg.segment(row))
			new_row = pd.Series()
			new_row['tokens'] = cut_res
			pos_res = ltp_pos.postag(new_row['tokens'])
			new_row['flags'] = list(pos_res)
			return new_row

		sentence_cut = df[col].apply(_cut)
		return sentence_cut


	def parallel_cut(self,df, col):
		n_cpu = mp.cpu_count()
		with mp.Pool(processes=n_cpu) as p:
			split_dfs = np.array_split(df, n_cpu)

			if self.cfg.cut_word_method == 'jieba':
				pool_results = p.map(partial(self._apply_cut_jieba, col=col), split_dfs)
			elif self.cfg.cut_word_method == 'pyltp':
				pool_results = p.map(partial(self._apply_cut_pyltp, col=col), split_dfs)
			else:
				pool_results = p.map(partial(self._apply_cut_jieba, col=col), split_dfs)

		# merging parts processed by different processes
		res = pd.concat(pool_results, axis=0)
		return res

	@staticmethod
	def clean_token(article_df: pd.DataFrame, qa_df: pd.DataFrame):
		"""
		clean data on token level
		:param article_df:
		:param qa_df:
		:return:
		"""

		def clean(row, token_col, flag_col):
			tokens_cleaned = []
			flags_cleaned = []
			for token, flag in zip(row[token_col], row[flag_col]):
				token = token.strip()
				if token != '':
					tokens_cleaned.append(token)
					flags_cleaned.append(flag)

			row[token_col] = tokens_cleaned
			row[flag_col] = flags_cleaned

			return row

		article_df = article_df.apply(lambda row: clean(row, 'article_tokens', 'article_flags'), axis=1)
		qa_df = qa_df.apply(lambda row: clean(row, 'question_tokens', 'question_flags'), axis=1)
		if 'answer_tokens' in qa_df.columns:
			qa_df = qa_df.apply(lambda row: clean(row, 'answer_tokens', 'answer_flags'), axis=1)

		return article_df, qa_df

	@staticmethod
	def _apply_sample_article(df: pd.DataFrame, article_tokens_col, article_flags_col,
							  question_tokens_col,
							  max_token_len=400):
		def _sample_article(row, article_tokens_col, article_flags_col, question_tokens_col, max_token_len=400):

			"""

			:param row:
			:param article_tokens_col:
			:param article_flags_col:
			:param question_tokens_col:
			:param max_token_len:
			:return:
			"""
			article_tokens = row[article_tokens_col]
			article_flags = row[article_flags_col]
			question_tokens = row[question_tokens_col]

			if len(article_tokens) <= max_token_len:
				return row
			sentences, sentences_f = [], []
			cur_s, cur_s_f = [], []
			question = ''.join(question_tokens)

			cand, cand_f = [], []
			rl = RougeL()
			for idx, (token, flag) in enumerate(zip(article_tokens, article_flags)):
				cur_s.append(token)
				cur_s_f.append(flag)

				if token in '。' or idx == len(article_tokens) - 1:
					if len(cur_s) >= 2:
						sentences.append(cur_s)
						sentences_f.append(cur_s_f)
						cur_s_str = ''.join(cur_s)
						rl.add_inst(cur_s_str, question)
						if rl.p_scores[-1] == 1.0:
							rl.r_scores[-1] = 1.0
					cur_s, cur_s_f = [], []
					continue

			if '。' not in ''.join(article_tokens):
				row[article_tokens_col] = sentences[0]
				row[article_flags_col] = sentences_f[0]
				return row

			scores = rl.r_scores
			s_rank = np.zeros(len(sentences))
			arg_sorted = list(reversed(np.argsort(scores)))

			for i in range(10):
				if i >= len(sentences):
					break
				pos = arg_sorted[i]
				score = scores[pos]
				if pos in [0, 1, len(sentences) - 1, len(sentences) - 2] or score == 0:
					continue
				block_scores = np.array([0.5 * score, 0.9 * score, score, score, 0.9 * score, 0.5 * score, 0.4 * score])
				# block_scores = np.array([0.25*score, 0.5*score, score, 0.8*score, 0.64*score, 0.512*score, 0.4096*score])
				block = s_rank[pos - 2: pos + 5]
				block_scores = block_scores[:len(block)]
				block_scores = np.max(np.stack([block_scores, block]), axis=0)
				s_rank[pos - 2: pos + 5] = block_scores

			rank = list(reversed(np.argsort(s_rank)))
			flag = [0 for i in range(len(sentences))]
			flag[0], flag[1], flag[-1], flag[-2] = 1, 1, 1, 1
			cur_len = len(sentences[0]) + len(sentences[1]) + len(sentences[-1]) + len(sentences[-2])

			for pos in rank:
				if cur_len < max_token_len:
					if s_rank[pos] > 0:
						flag[pos] = 1
						cur_len += len(sentences[pos])
				else:
					break

			for i in range(len(flag)):
				if flag[i] != 0:
					cand.extend(sentences[i])
					cand_f.extend(sentences_f[i])

			row[article_tokens_col] = cand[:max_token_len]
			row[article_flags_col] = cand_f[:max_token_len]

			return row

		df = df.apply(
			lambda row: _sample_article(row, article_tokens_col, article_flags_col, question_tokens_col, max_token_len),
			axis=1)

		return df

	def parallel_sample_article(self, article_df: pd.DataFrame, qa_df: pd.DataFrame, max_token_len=400):
		sample_df = pd.merge(article_df, qa_df, how='inner', on=['article_id'])
		n_cpu = mp.cpu_count()
		with mp.Pool(processes=n_cpu) as p:
			split_dfs = np.array_split(sample_df, n_cpu)
			pool_results = p.map(partial(self._apply_sample_article,
										 article_tokens_col='article_tokens',
										 article_flags_col='article_flags',
										 question_tokens_col='question_tokens',
										 max_token_len=max_token_len), split_dfs)

		# merging parts processed by different processes
		res = pd.concat(pool_results, axis=0)

		return res

	@staticmethod
	def _apply_find_gold_span(sample_df: pd.DataFrame, article_tokens_col, question_tokens_col, answer_tokens_col):
		def _find_golden_span(row, article_tokens_col, question_tokens_col, answer_tokens_col):

			article_tokens = row[article_tokens_col]
			question_tokens = row[question_tokens_col]
			answer_tokens = row[answer_tokens_col]
			row['answer_token_start'] = -1
			row['answer_token_end'] = -1
			row['delta_token_starts'] = []
			row['delta_token_ends'] = []
			row['delta_rouges'] = []
			rl = RougeL()
			rl_q = RougeL()
			ground_ans = ''.join(answer_tokens).strip()
			questrin_str = ''.join(question_tokens).strip()
			len_p = len(article_tokens)
			len_a = len(answer_tokens)
			s2 = set(ground_ans)
			star_spans = []
			end_spans = []
			rl_q_idx = []
			for i in range(len_p - len_a + 1):
				for t_len in range(len_a - 2, len_a + 3):
					if t_len <= 0 or i + t_len > len_p:
						continue
					cand_ans = ''.join(article_tokens[i:i + t_len]).strip()
					s1 = set(cand_ans)
					mlen = max(len(s1), len(s2))
					iou = len(s1.intersection(s2)) / mlen if mlen != 0 else 0.0
					if iou >= 0.25:
						rl.add_inst(cand_ans, ground_ans)
						if rl.inst_scores[-1] == 1.0:
							s = max(i - 7, 0)
							cand_ctx = ''.join(article_tokens[s:i + t_len + 3]).strip()
							rl_q.add_inst(cand_ctx, questrin_str)
							rl_q_idx.append(len(star_spans))

						star_spans.append(i)
						end_spans.append(i + t_len - 1)
			if len(star_spans) == 0:
				return row
			else:
				score = np.array(rl.inst_scores)
				em_mask = (score == 1.0)

				if em_mask.sum() <= 1:
					best_idx = np.argmax(rl.inst_scores)
				else:
					best_idx = rl_q_idx[int(np.argmax(rl_q.r_scores))]

				row['answer_token_start'] = star_spans[best_idx]
				row['answer_token_end'] = end_spans[best_idx]
				row['delta_token_starts'] = star_spans
				row['delta_token_ends'] = end_spans
				row['delta_rouges'] = rl.inst_scores

			return row

		def _find_golden_span_v2(row, article_tokens_col, question_tokens_col, answer_tokens_col):

			article_tokens = row[article_tokens_col]
			question_tokens = row[question_tokens_col]
			answer_tokens = row[answer_tokens_col]
			row['answer_token_start'] = -1
			row['answer_token_end'] = -1
			rl_ans = RougeL()
			rl_q = RougeL()
			ground_ans = ''.join(answer_tokens).strip()
			questrin_str = ''.join(question_tokens).strip()
			len_p = len(article_tokens)
			len_a = len(answer_tokens)
			s2 = set(ground_ans)
			spans = []
			for i in range(len_p - len_a + 1):
				for t_len in range(len_a - 2, len_a + 3):
					if t_len == 0 or i + t_len > len_p:
						continue
					cand_ans = ''.join(article_tokens[i:i + t_len]).strip()

					s1 = set(cand_ans)
					mlen = max(len(s1), len(s2))
					iou = len(s1.intersection(s2)) / mlen if mlen != 0 else 0.0
					if iou > 0.3:
						s = max(i - 5, 0)
						cand_ctx = ''.join(article_tokens[s:i + t_len + 5]).strip()
						rl_ans.add_inst(cand_ans, ground_ans)
						rl_q.add_inst(cand_ctx, questrin_str)
						spans.append([i, i + t_len - 1])

			if len(spans) == 0:
				return row

			sim_ans = np.array(rl_ans.inst_scores)
			sim_q = np.array(rl_q.r_scores)

			total_score = 0.7 * sim_ans + 0.3 * sim_q

			best_idx = total_score.argmax()

			row['answer_token_start'] = spans[best_idx][0]
			row['answer_token_end'] = spans[best_idx][1]

			return row

		sample_df = sample_df.apply(
			lambda row: _find_golden_span(row,
										  article_tokens_col,
										  question_tokens_col,
										  answer_tokens_col),
			axis=1)

		return sample_df


	def parallel_find_gold_span(self, sample_df: pd.DataFrame):
		n_cpu = mp.cpu_count()
		with mp.Pool(processes=n_cpu) as p:
			split_dfs = np.array_split(sample_df, n_cpu)
			pool_results = p.map(partial(self._apply_find_gold_span,
										 article_tokens_col='article_tokens',
										 question_tokens_col='question_tokens',
										 answer_tokens_col='answer_tokens'),
								 split_dfs)

		# merging parts processed by different processes
		res = pd.concat(pool_results, axis=0)

		return res


	def preprocess_dataset(self, raw_path):



		adf, qadf = self.trans_to_df(raw_path)
		adf, qadf = self.clean_text(adf, qadf)

		article_cut = self.parallel_cut(adf, 'article')
		adf.drop(['article'], axis=1, inplace=True)
		adf['article_tokens'] = article_cut['tokens']
		adf['article_flags'] = article_cut['flags']

		question_cut = self.parallel_cut(qadf, 'question')
		qadf.drop(['question'], axis=1, inplace=True)
		qadf['question_tokens'] = question_cut['tokens']
		qadf['question_flags'] = question_cut['flags']

		if 'answer' in qadf.columns:
			ans_cut = self.parallel_cut(qadf, 'answer')
			qadf['answer_tokens'] = ans_cut['tokens']
			qadf['answer_flags'] = ans_cut['flags']

		adf, qadf = self.clean_token(adf, qadf)
		if 'answer' in qadf.columns:
			sample_df = self.parallel_sample_article(adf, qadf, self.cfg.article_sample_len)
			sample_df = self.parallel_find_gold_span(sample_df)
		else:
			sample_df = self.parallel_sample_article(adf, qadf, self.cfg.article_sample_len_test)


		croups = list(adf['article_tokens']) + list(qadf['question_tokens'])
		flag_croups = list(adf['article_flags']) + list(qadf['question_flags'])
		if 'answer' in qadf.columns:
			croups += list(qadf['answer_tokens'])
			flag_croups += list(qadf['answer_flags'])
		sample_df = sample_df.to_dict(orient='records')

		return croups, flag_croups, sample_df

	def release(self):
		if self.ltp_pos:
			self.ltp_pos.release()
		if self.ltp_seg:
			self.ltp_seg.release()

	"""
	if __name__ == '__main__':
	
		adf, qadf = trans_to_df('./data/train/question.json')
		adf, qadf = clean_text(adf, qadf)
	
		article_cut = parallel_cut(adf, 'article')
		adf.drop(['article'], axis=1, inplace=True)
		adf['article_tokens'] = article_cut['tokens']
		adf['article_flags'] = article_cut['flags']
	
		question_cut = parallel_cut(qadf, 'question')
		qadf.drop(['question'], axis=1, inplace=True)
		qadf['question_tokens'] = question_cut['tokens']
		qadf['question_flags'] = question_cut['flags']
	
		ans_cut = parallel_cut(qadf, 'answer')
		qadf['answer_tokens'] = ans_cut['tokens']
		qadf['answer_flags'] = ans_cut['flags']
	
		adf, qadf = clean_token(adf, qadf)
	
		sample_df = parallel_sample_article(adf, qadf)
	
		sample_df_with_span = parallel_find_gold_span(sample_df.iloc[:10])
	"""
