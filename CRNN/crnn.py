import os
import time
import numpy as np
import tensorflow as tf
import config
from scipy.misc import imread, imresize, imsave
from tensorflow.contrib import rnn

from data_manager import DataManager
from utils import sparse_tuple_from, resize_image, label_to_array, ground_truth_to_word, levenshtein

# 日志级别
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"


class CRNN(object):
    def __init__(self, batch_size, model_path, examples_path, max_image_width, train_test_ratio, restore):
        self.step = 0
        self.__model_path = model_path
        self.__save_path = os.path.join(model_path, 'ckp')

        self.__restore = restore

        self.__train_name = str(int(time.time()))
        self.__session = tf.Session()  # ????????????????????? 不理解

        # 构建图
        with self.__session.as_default():
            (
                self.__inputs,
                self.__targets,
                self.__seq_len,
                self.__logits,
                self.__decoded,
                self.__optimizer,
                self.__acc,
                self.__cost,
                self.__max_char_count,
                self.__init
            ) = self.crnn(max_image_width, batch_size)

            self.__init.run()

        with self.__session.as_default():
            self.__saver = tf.train.Saver(tf.global_variables(), max_to_keep=10)  ## ？？？
            # 载入上次的ckpt如果有需要
            if self.__restore:
                print("Restoring")
                ckpt = tf.train.latest_checkpoint(self.__model_path)
                if ckpt:
                    print("Checkpoint is valid")
                    self.step = int(ckpt.split('-')[1])
                    self.__saver.restore(self.__session, ckpt)

        #  创建 data_manager
        self.__data_manager = DataManager(batch_size, model_path, examples_path, max_image_width, train_test_ratio,
                                          self.__max_char_count)

    def crnn(self, max_width, batch_size):
        def BidirectionalRNN(inputs, seq_len):
            """
            双向lstm部分：两层双向lstm叠加
            :param inputs:
            :param seq_len:
            :return:
            """

            with tf.variable_scope(None, default_name="bidirectional-rnn-1"):
                # 前向
                lstm_fw_cell_1 = tf.nn.rnn_cell.LSTMCell(256)
                # 反向
                lstm_bw_cell_1 = tf.nn.rnn_cell.LSTMCell(256)
                # 中间输出
                inter_output, _ = tf.nn.bidirectional_dynamic_rnn(lstm_fw_cell_1, lstm_bw_cell_1, inputs, seq_len,
                                                                  dtype=tf.float32)

                inter_output = tf.concat(inter_output, 2)

            with tf.variable_scope(None, default_name="bidirectional-rnn-2"):
                # 前向
                lstm_fw_cell_2 = tf.nn.rnn_cell.LSTMCell(256)
                # 反向
                lstm_bw_cell_2 = tf.nn.rnn_cell.LSTMCell(256)

                outputs, _ = tf.nn.bidirectional_dynamic_rnn(lstm_fw_cell_2, lstm_bw_cell_2, inter_output, seq_len,
                                                             dtype=tf.float32)

                outputs = tf.concat(outputs, 2)

                return outputs

        def CNN(inputs):
            """
             卷积神经网络部分
            :param inputs:
            :return:
            """

            # 64 / 3x3 / 1 / 1
            conv1 = tf.layers.conv2d(inputs, filters=64, kernel_size=(3, 3), padding="same", activation=tf.nn.relu)

            # 2x2 / 1
            pool1 = tf.layers.max_pooling2d(inputs=conv1, pool_size=[2, 2], strides=2)

            # 128 / 3x3 / 1 / 1
            conv2 = tf.layers.conv2d(pool1, filters=128, kernel_size=(3, 3), padding="same", activation=tf.nn.relu)

            # 2x2 / 1
            pool2 = tf.layers.max_pooling2d(inputs=conv2, pool_size=[2, 2], strides=2)

            # 256 / 3x3 / 1 / 1
            conv3 = tf.layers.conv2d(inputs=pool2, filters=256, kernel_size=(3, 3), padding="same",
                                     activation=tf.nn.relu)

            # Batch normalization layer
            bnorm1 = tf.layers.batch_normalization(conv3)

            # 256 / 3x3 / 1 / 1
            conv4 = tf.layers.conv2d(inputs=bnorm1, filters=256, kernel_size=(3, 3), padding="same",
                                     activation=tf.nn.relu)

            # 1x2 / 1
            pool3 = tf.layers.max_pooling2d(inputs=conv4, pool_size=[2, 2], strides=[1, 2], padding="same")

            # 512 / 3x3 / 1 / 1
            conv5 = tf.layers.conv2d(inputs=pool3, filters=512, kernel_size=(3, 3), padding="same",
                                     activation=tf.nn.relu)

            # Batch normalization layer
            bnorm2 = tf.layers.batch_normalization(conv5)

            # 512 / 3x3 / 1 / 1
            conv6 = tf.layers.conv2d(inputs=bnorm2, filters=512, kernel_size=(3, 3), padding="same",
                                     activation=tf.nn.relu)

            # 1x2 / 2
            pool4 = tf.layers.max_pooling2d(inputs=conv6, pool_size=[2, 2], strides=[1, 2], padding="same")

            # 512 / 2x2 /1/ 0
            conv7 = tf.layers.conv2d(inputs=pool4, filters=512, kernel_size=(2, 2), padding="valid",
                                     activation=tf.nn.relu)

            return conv7

        inputs = tf.placeholder(tf.float32, [batch_size, max_width, 32, 1])

        # 目标输出
        targets = tf.sparse_placeholder(tf.int32, name="targets")

        # 序列长度
        seq_len = tf.placeholder(tf.int32, [None], name="seq_len")

        cnn_output = CNN(inputs)

        reshaped_cnn_output = tf.reshape(cnn_output, [batch_size, -1, 512])

        max_char_count = reshaped_cnn_output.get_shape().as_list()[1]

        crnn_model = BidirectionalRNN(reshaped_cnn_output, seq_len)

        logits = tf.reshape(crnn_model, [-1, 512])

        W = tf.Variable(tf.truncated_normal([512, config.NUM_CLASSES], stddev=0.1), name="W")
        b = tf.Variable(tf.constant(0., shape=[config.NUM_CLASSES]),name="b")

        logits = tf.matmul(logits, W) + b

        logits = tf.reshape(logits, [batch_size, -1, config.NUM_CLASSES])

        # 最终层,BLSTM的输出
        logits = tf.transpose(logits, (1, 0, 2))

        # loss and cost 计算
        loss = tf.nn.ctc_loss(targets, logits, seq_len)

        cost = tf.reduce_mean(loss)

        optimizer = tf.train.AdamOptimizer(learning_rate=0.0001).minimize(cost)

        # 解码
        decoded, log_prob = tf.nn.ctc_beam_search_decoder(logits, seq_len, merge_repeated=False)

        dense_decoded = tf.sparse_tensor_to_dense(decoded[0], default_value=-1)

        # 错误率
        acc = tf.reduce_mean(tf.edit_distance(tf.cast(decoded[0], tf.int32), targets))

        init = tf.global_variables_initializer()

        return inputs, targets, seq_len, logits, dense_decoded, optimizer, acc, cost, max_char_count, init

    def train(self, iteration_count):
        with self.__session.as_default():
            print("Training")
            for i in range(self.step, iteration_count + self.step):
                iter_loss = 0
                for batch_y, batch_dt, batch_x in self.__data_manager.train_batches:
                    # print(batch_y, "\n",batch_dt,"\n" ,batch_x)
                    op, decoded, loss_value = self.__session.run(
                        [self.__optimizer, self.__decoded, self.__cost],
                        feed_dict={
                            self.__inputs: batch_x,
                            self.__seq_len: [self.__max_char_count] * self.__data_manager.batch_size,
                            self.__targets: batch_dt
                        }
                    )

                    # 每10轮会打印一次信息，从第一轮开始
                    if i % 10 == 0:
                        # 打印第一个batch的训练结果
                        for j in range(1):
                            print(batch_y[j])
                            print(ground_truth_to_word(decoded[j]))

                    iter_loss += loss_value

                    self.__saver.save(
                        self.__session,
                        self.__save_path,
                        global_step=self.step
                    )

                print('[{}] Iteration loss: {}'.format(self.step, iter_loss))

                self.step += 1
        return None

    def test(self):
        with self.__session.as_default():
            print('Testing')
            for batch_y, _, batch_x in self.__data_manager.test_batches:
                decoded = self.__session.run(
                    self.__decoded,
                    feed_dict={
                        self.__inputs: batch_x,
                        self.__seq_len: [self.__max_char_count] * self.__data_manager.batch_size
                    }
                )

                for i, y in enumerate(batch_y):
                    print(batch_y[i])
                    print(ground_truth_to_word(decoded[i]))
        return None
