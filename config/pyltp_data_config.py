from config.dataset_base_config import DataSetConfigBase


class DataSetConfigPyltp(DataSetConfigBase):
	cut_word_method = 'pyltp'

	train_preprocessed_file = DataSetConfigBase.root + 'data/train/train_preprocessed_' + cut_word_method + '_' + str(
		DataSetConfigBase.article_sample_len) + '.json'

	train_croups_file = DataSetConfigBase.root + 'data/train/train_croups_' + cut_word_method + '.json'

	train_flag_croups_file = DataSetConfigBase.root + 'data/train/train_flag_croups_' + cut_word_method + '.json'

	test_preprocessed_file = DataSetConfigBase.root + 'data/test/test_preprocessed_' + cut_word_method + '_' + str(
		DataSetConfigBase.article_sample_len_test) + '.json'

	test_croups_file = DataSetConfigBase.root + 'data/test/test_croups_' + cut_word_method + '.json'
	test_flag_croups_file = DataSetConfigBase.root + 'data/test/test_flag_croups_' + cut_word_method + '.json'

	high_prob_test_preprocessed_file = DataSetConfigBase.root + 'data/train/high_prob_test_preprocessed_' + cut_word_method + '_' + str(
		DataSetConfigBase.article_sample_len_test) + '.json'

	high_prob_test_croups_file = DataSetConfigBase.root + 'data/train/high_prob_test_croups_' + cut_word_method + '.json'
	high_prob_test_flag_croups_file = DataSetConfigBase.root + 'data/train/high_prob_test_flag_croups_' + cut_word_method + '.json'

	flag_embed_file = DataSetConfigBase.root + 'data/embedding/flag_embed' + str(
		DataSetConfigBase.flag_emb_dim) + '_' + cut_word_method \
					  + ('_highprob' if DataSetConfigBase.use_high_prob_test_sample else '')  \
					  + ('_withtest' if DataSetConfigBase.use_test_vocab else '') \
					  + '.wv'
	token_embed_file = DataSetConfigBase.root + 'data/embedding/token_embed' + str(
		DataSetConfigBase.token_emb_dim) + '_' + cut_word_method \
					   + ('_highprob' if DataSetConfigBase.use_high_prob_test_sample else '') \
				       + ('_withtest' if DataSetConfigBase.use_test_vocab else '') \
					   + '.wv'

	pyltp_cws_model_path = './data/ltp_data_v3.4.0/cws.model'
	pyltp_pos_model_path = './data/ltp_data_v3.4.0/pos.model'


config = DataSetConfigPyltp()
