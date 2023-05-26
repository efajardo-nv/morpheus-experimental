# SPDX-FileCopyrightText: Copyright (c) 2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

import cupy as cp
import torch
import utils
from dataloader import DataLoader
from detector import Detector
from dga_dataset import DGADataset
from rnn_classifier import RNNClassifier
from sklearn.model_selection import train_test_split
from tqdm import trange

import cudf

log = logging.getLogger(__name__)


class DGADetector(Detector):
    """
    This class provides multiple functionalities such as build, train and evaluate the RNNClassifier model
    to distinguish legitimate and DGA domain names.
    """

    def init_model(self, char_vocab=128, hidden_size=100, n_domain_type=2, n_layers=3):
        """
        This function instantiates RNNClassifier model to train. And also optimizes to scale it and keep running on
        parallelism.

        :param char_vocab: Vocabulary size is set to 128 ASCII characters.
        :type char_vocab: int
        :param hidden_size: Hidden size of the network.
        :type hidden_size: int
        :param n_domain_type: Number of domain types.
        :type n_domain_type: int
        :param n_layers: Number of network layers.
        :type n_layers: int
        """
        if self.model is None:
            model = RNNClassifier(char_vocab, hidden_size, n_domain_type, n_layers)
            self.leverage_model(model)

    def load_checkpoint(self, file_path):
        """ This function load already saved model checkpoint and sets cuda parameters.

        :param file_path: File path of a model checkpoint to be loaded.
        :type file_path: string
        """
        checkpoint = torch.load(file_path)
        model = RNNClassifier(
            checkpoint["input_size"],
            checkpoint["hidden_size"],
            checkpoint["output_size"],
            checkpoint["n_layers"],
        )
        model.load_state_dict(checkpoint["state_dict"])
        super().leverage_model(model)

    def save_checkpoint(self, file_path):
        """ This function saves model checkpoint to given location.

        :param file_path: File path to save model checkpoint.
        :type file_path: string
        """
        model = self.get_unwrapped_model()
        checkpoint = {
            "state_dict": model.state_dict(),
            "input_size": model.input_size,
            "hidden_size": model.hidden_size,
            "n_layers": model.n_layers,
            "output_size": model.output_size,
        }
        super()._save_checkpoint(checkpoint, file_path)

    def train_model(self, train_data, labels, batch_size=1000, epochs=5, train_size=0.7, truncate=100, pad_max_len=100):
        """
        This function is used for training RNNClassifier model with a given training dataset. It returns total loss to
        determine model prediction accuracy.

        :param train_data: Training data
        :type train_data: cudf.Series
        :param labels: labels data
        :type labels: cudf.Series
        :param batch_size: batch size
        :type batch_size: int
        :param epochs: Number of epochs for training
        :type epochs: int
        :param train_size: Training size for splitting training and test data
        :type train_size: int
        :param truncate: Truncate string to n number of characters.
        :type truncate: int

        Examples
        --------
        >>> from clx.analytics.dga_detector import DGADetector
        >>> dd = DGADetector()
        >>> dd.init_model()
        >>> dd.train_model(train_data, labels)
        1.5728906989097595
        """
        log.info("Initiating model training ...")
        log.info('Truncate domains to width: {}'.format(truncate))

        self.model.train()
        train_dataloader, test_dataloader = self._preprocess_data(train_data, labels, batch_size, train_size, truncate)

        for _ in trange(epochs, desc="Epoch"):
            total_loss = 0
            i = 0
            for df in train_dataloader.get_chunks():
                domains_len = df.shape[0]
                if domains_len > 0:
                    types_tensor = self._create_types_tensor(df["type"])
                    df = df.drop(["type", "domain"], axis=1)
                    input, seq_lengths = self._create_variables(df, pad_max_len)
                    model_result = self.model(input, seq_lengths)
                    loss = self._get_loss(model_result, types_tensor)
                    total_loss += loss
                    i = i + 1
                    if i % 10 == 0:
                        log.info("[{}/{} ({:.0f}%)]\tLoss: {:.2f}".format(
                            i * domains_len,
                            train_dataloader.dataset_len,
                            100.0 * i * domains_len / train_dataloader.dataset_len,
                            total_loss / i * domains_len,
                        ))
            self.evaluate_model(test_dataloader)

    def predict(self, domains, probability=False, truncate=100, pad_max_len=100):
        """
        This function accepts cudf series of domains as an argument to classify domain names as benign/malicious
        and returns the learned label for each object in the form of cudf series.

        Parameters
        ----------
        domains : cudf.Series
            List of domains
        probability : bool
            Whether to return probabilities. Default is False.
        truncate : int
            Truncate string to this number of characters.

        Returns
        -------
        cudf.Series
            Predicted results with respect to given domains.

        Examples
        --------
        >>> dd.predict(['nvidia.com', 'dgadomain'])
        0    0.010
        1    0.924
        Name: dga_probability, dtype: decimal
        """
        log.debug("Initiating model inference ...")
        self.model.eval()
        df = cudf.DataFrame({"domain": domains})
        log.debug('Truncate domains to width: {}'.format(truncate))
        df['domain'] = df['domain'].str.slice_replace(truncate, repl='')
        temp_df = utils.str2ascii(df, 'domain')
        # Assigning sorted domains index to return learned labels as per the given input order.
        df.index = temp_df.index
        df["domain"] = temp_df["domain"]
        temp_df = temp_df.drop("domain", axis=1)
        input, seq_lengths = self._create_variables(temp_df, pad_max_len)
        del temp_df
        model_result = self.model(input, seq_lengths)
        if probability:
            model_result = model_result[:, 0]
            preds = torch.sigmoid(model_result)
            preds = preds.view(-1).tolist()
            df["preds"] = preds
        else:
            preds = model_result.data.max(1, keepdim=True)[1]
            preds = preds.view(-1).tolist()
            df["preds"] = preds
        df = df.sort_index()
        return df["preds"]

    def _create_types_tensor(self, type_series):
        """Create types tensor variable in the same order of sequence tensor"""
        types = type_series.values_host
        types_tensor = torch.LongTensor(types)
        if torch.cuda.is_available():
            types_tensor = self._set_var2cuda(types_tensor)
        return types_tensor

    def _create_variables(self, df, pad_max_len):
        """
        Creates vectorized sequence for given domains and wraps around cuda for parallel processing.
        """
        seq_len_arr = df["len"].values_host
        df = df.drop("len", axis=1)
        seq_len_tensor = torch.LongTensor(seq_len_arr)

        # seq_tensor = self._df2tensor(df)
        # seq_cp = cp.asarray(df.to_cupy()).astype("long")
        seq_cp = df.to_cupy()
        input = cp.zeros((seq_cp.shape[0], pad_max_len))
        input[:seq_cp.shape[0], :seq_cp.shape[1]] = seq_cp
        input = input.astype("long")
        seq_tensor = torch.as_tensor(input)

        # Return variables
        # DataParallel requires everything to be a Variable
        if torch.cuda.is_available():
            seq_tensor = self._set_var2cuda(seq_tensor)
            seq_len_tensor = self._set_var2cuda(seq_len_tensor)
        return seq_tensor, seq_len_tensor

    def evaluate_model(self, dataloader, pad_max_len=100):
        """
        This function evaluates the trained model to verify it's accuracy.

        Parameters
        ----------
        dataloader : DataLoader
            Instance holds preprocessed data.
        probability : bool
            Whether to return probabilities. Default is False.
        truncate : int
            Truncate string to this number of characters.

        Returns
        -------
        float
            Model accuracy

        Examples
        --------
        >>> dd = DGADetector()
        >>> dd.init_model()
        >>> dd.evaluate_model(dataloader)
        Evaluating trained model ...
        Test set accuracy: 3/4 (0.75)
        """
        log.info("Evaluating trained model ...")
        correct = 0
        for df in dataloader.get_chunks():
            target = self._create_types_tensor(df["type"])
            df = df.drop(["type", "domain"], axis=1)
            input, seq_lengths = self._create_variables(df, pad_max_len)
            output = self.model(input, seq_lengths)
            pred = output.data.max(1, keepdim=True)[1]
            correct += pred.eq(target.data.view_as(pred)).cpu().sum()
        accuracy = float(correct) / dataloader.dataset_len
        log.info("Test set accuracy: {}/{} ({})\n".format(correct, dataloader.dataset_len, accuracy))
        return accuracy

    def _get_loss(self, model_result, types_tensor):
        loss = self.criterion(model_result, types_tensor)
        self.model.zero_grad()
        loss.backward()
        self.optimizer.step()
        return loss.item()

    def _set_var2cuda(self, tensor):
        """
        Set variable to cuda.
        """
        return tensor.cuda()

    def _preprocess_data(self, train_data, labels, batch_size, train_size, truncate):
        train_gdf = cudf.DataFrame()
        train_gdf["domain"] = train_data
        train_gdf["type"] = labels
        train, test = train_test_split(train_gdf, train_size=train_size)
        test_df = self._create_df(test, test["type"])
        train_df = self._create_df(train, train["type"])

        test_dataset = DGADataset(test_df, truncate)
        train_dataset = DGADataset(train_df, truncate)

        test_dataloader = DataLoader(test_dataset, batchsize=batch_size)
        train_dataloader = DataLoader(train_dataset, batchsize=batch_size)
        return train_dataloader, test_dataloader

    def _create_df(self, domain_df, type_series):
        df = cudf.DataFrame()
        df["domain"] = domain_df["domain"].reset_index(drop=True)
        df["type"] = type_series.reset_index(drop=True)
        return df
