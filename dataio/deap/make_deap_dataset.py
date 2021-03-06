import numpy as np
import scipy.stats
import pickle
import tensorflow as tf
import os
import itertools
import more_itertools as mit
import random
from tqdm import tqdm
import json
import pywt
from scipy import signal

from typing import List

np.random.seed(42)
random.seed(42)


class DEAPWriter:

    @staticmethod
    def bytes_feature(value):
        """Returns a bytes_list from a string / byte."""
        if isinstance(value, type(tf.constant(0))):
            # BytesList won't unpack a string from an EagerTensor.
            value = value.numpy()
        return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))

    @staticmethod
    def float_feature(value):
        """Returns a float_list from a float / double."""
        return tf.train.Feature(float_list=tf.train.FloatList(value=[value]))

    @staticmethod
    def int64_feature(value):
        """Returns an int64_list from a bool / enum / int / uint."""
        return tf.train.Feature(int64_list=tf.train.Int64List(value=[value]))

    @staticmethod
    def numpy_feature(value):
        """serializes arbitrary tensors and returns a byte_list. Use with numpy arrays."""
        return DEAPWriter.bytes_feature(tf.io.serialize_tensor(value))

    def __init__(self, filenames):
        self.filenames = filenames
        self.records_processed = [0] * len(filenames)

    def get_filenames(self):
        return ", ".join(self.filenames)

    def __enter__(self):
        self.writers = [tf.io.TFRecordWriter(fn) for fn in self.filenames]
        self.writer_iterator = itertools.cycle(self.writers)
        print(f"Opened TFRecord files{', '.join(self.filenames)}")
        return self

    def __exit__(self, type, value, traceback):
        for i, t in enumerate(zip(self.filenames, self.writers)):
            filename, writer = t
            print(
                f"Closing {filename} after {self.records_processed[i]} records")
            writer.close()

    def serialize_example(self, data, labels):
        feature = {
            "X": DEAPWriter.numpy_feature(data)
        }
        for ln in labels.keys():
            feature[ln] = DEAPWriter.int64_feature(labels[ln])
        example_proto = tf.train.Example(
            features=tf.train.Features(feature=feature))
        return example_proto.SerializeToString()

    def write_example(self, data, labels):
        self.shape = data.shape
        serialized = self.serialize_example(data, labels)
        this_writer = next(self.writer_iterator)
        writer_id = self.writers.index(this_writer)
        this_writer.write(serialized)
        self.records_processed[writer_id] += 1


class DEAPWriterBuffer:

    def __init__(self, dw, item_shape, label_shape, buffer_size):
        self.data_buffer_shape = tuple([buffer_size] + item_shape)
        self.label_buffer_shape = tuple([buffer_size] + label_shape)
        self.data_buffer = np.zeros(self.data_buffer_shape)
        self.label_buffer = np.zeros(self.label_buffer_shape, dtype=int)
        self.writer = dw
        self.collected = 0
        self.label_names = ["valence", "arousal", "dominance", "liking"]

    def labels_np_to_dict(self, np):
        return {k: v for k, v in zip(self.label_names, list(np))}

    def flush_buffer(self):
        print(
            f"flushing buffer for writer {self.writer.get_filenames()}")
        permutation = np.random.permutation(self.collected)
        # np.random.choice(range(num_collected),
        #                 num_collected,
        #                 replace=False)
        self.data_buffer = self.data_buffer[permutation]
        self.label_buffer = self.label_buffer[permutation]
        for i in tqdm(range(self.collected)):
            d = self.data_buffer[i]
            l = self.label_buffer[i]
            ld = self.labels_np_to_dict(l)
            # data, labels = self.buffer[s]
            # self.writer.write_example(data, labels)
            self.writer.write_example(d, ld)
        print("flushed buffer")

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.flush_buffer()

    def write_example(self, data, labels):
        self.data_buffer[self.collected] = data
        self.label_buffer[self.collected] = labels
        self.collected += 1
        # self.buffer.append((data, labels))
        # if len(self.buffer) == self.buffer_size:
        #     self.flush_buffer()


class LabelTransformer:

    def __init__(self,
                 valence_classes: int,
                 arousal_classes: int,
                 dominance_classes: int,
                 liking_classes: int,
                 label_names):
        self.valence_classes = self.build_classes(valence_classes)
        self.arousal_classes = self.build_classes(arousal_classes)
        self.dominance_classes = self.build_classes(dominance_classes)
        self.liking_classes = self.build_classes(liking_classes)
        self.label_names = label_names

    def build_classes(self, num_classes: int, min_value=1.0, max_value=9):
        breaks = np.linspace(min_value, max_value, num_classes + 1)
        a, b = itertools.tee(breaks)
        next(b, None)
        return list(zip(a, b))

    def transform_labels(self, labels, return_np=False):
        valence_label = self.transform_label_channel_with_classes(
            labels[0], self.valence_classes)
        arousal_label = self.transform_label_channel_with_classes(
            labels[1], self.valence_classes)
        dominance_label = self.transform_label_channel_with_classes(
            labels[2], self.valence_classes)
        liking_label = self.transform_label_channel_with_classes(
            labels[3], self.valence_classes)
        if return_np:
            return np.array([valence_label, arousal_label, dominance_label, liking_label])
        return {k: v for k, v in zip(self.label_names,
                                     [valence_label, arousal_label,
                                      dominance_label, liking_label])
                }

    def transform_label_channel_with_classes(self, label_channel, classes):
        for class_id, class_tuple in enumerate(classes):
            min_inclusive, max_exclusive = class_tuple
            if (label_channel >= min_inclusive) and (label_channel < max_exclusive):
                label_channel = class_id
                break
        # last class is including the (last) maximum value (9.0)
        if label_channel == max_exclusive:
            label_channel = len(classes) - 1
        # one_hot = LabelTransformer.onehot(len(classes), label_channel)
        # return one_hot
        # return as scalar
        return label_channel

    @staticmethod
    def onehot(n_classes, labels):
        return np.eye(n_classes)[labels]


def process_directory(
    input_dir,
    target_directory,
    out_names,
    mode,
    target_seconds,
    trim_samples,
    validation_split=0.2,
    num_classes=(2, 2, 2, 2),
    num_experiments=40,
    processing=None,
    window_step=None
):
    file_list = os.listdir(input_dir)
    num_subjects = len(list(x for x in file_list if '.dat' in x))
    samples_per_chunk = int(128 * target_seconds)
    if window_step is not None:
        step_samples = 128 * window_step

    total_samples_per_experiment = (63 * 128 - trim_samples)

    if window_step is not None:
        windows = list(mit.windowed(
            range(total_samples_per_experiment), samples_per_chunk, step=step_samples))
    else:
        windows = list(mit.windowed(range(total_samples_per_experiment),
                                    samples_per_chunk, step=samples_per_chunk))

    num_samples_per_experiment = len(windows)

    file_dict = {}
    if mode == 'subject-dependent':
        total_number_available = int(num_subjects *
                                     num_experiments * num_samples_per_experiment)
        num_validation = int(total_number_available * validation_split)
        num_training = total_number_available - num_validation
        print(
            f"total {total_number_available} samples, train {num_training}, validation {num_validation}")
        validation_indices = np.random.choice(
            total_number_available, size=num_validation, replace=False)
        train_names = [x for x in out_names if "train" in x]
        test_names = [x for x in out_names if "test" in x]

        current_sample = np.array(0)

        data_shape = [40, samples_per_chunk]
        label_shape = [4]

        with DEAPWriter(train_names) as train_writer, DEAPWriter(test_names)as test_writer:
            with DEAPWriterBuffer(train_writer,
                                  data_shape,
                                  label_shape,
                                  num_training) as train_writer_buffer, \
                DEAPWriterBuffer(test_writer,
                                 data_shape,
                                 label_shape,
                                 num_validation) as test_writer_buffer:
                for file in tqdm(file_list):
                    abs_file = os.path.join(input_dir, file)
                    process_file_subject_dependent(in_file=abs_file,
                                                   target_directory=target_directory,
                                                   train_writer=train_writer_buffer,
                                                   test_writer=test_writer_buffer,
                                                   current_sample=current_sample,
                                                   validation_indices=validation_indices,
                                                   target_windows=windows,
                                                   trim_samples=trim_samples,
                                                   num_classes=num_classes,
                                                   processing=processing)
            shape = train_writer.shape
            records_available = {"train": total_number_available - num_validation,
                                 "test": num_validation,
                                 "total": total_number_available}

    if mode == 'subject-independent':
        file_list.sort()
        available_per_file = len(windows) * num_experiments
        num_test_files = round(validation_split * len(file_list))
        num_train_files = len(file_list) - num_test_files
        test_names = [out_names[i] for i, _ in enumerate(
            file_list) if i < num_test_files]
        train_names = [x for x in out_names if x not in test_names]
        for file_id, file in enumerate(tqdm(file_list)):
            abs_file = os.path.join(input_dir, file)
            tffile = out_names[file_id]
            valence_classes, arousal_classes, dominance_classes, liking_classes = num_classes
            with DEAPWriter([tffile]) as writer:
                outname = process_file(
                    pickle_file=abs_file,
                    writer=writer,
                    method='own',
                    file_id=file_id,
                    trim_samples=trim_samples,
                    target_splits=num_samples_per_experiment,
                    valence_classes=valence_classes,
                    arousal_classes=arousal_classes,
                    dominance_classes=dominance_classes,
                    liking_classes=liking_classes,
                    progress_bar=None,
                    axes_order='sensors-first'
                )
                shape = writer.shape

        records_available = {"train": available_per_file * num_train_files,
                             "test": available_per_file * num_test_files,
                             "total": available_per_file * (num_test_files + num_train_files)}

    file_dict["train"] = train_names
    file_dict["test"] = test_names
    file_dict["validation_split"] = validation_split
    file_dict["available"] = records_available

    write_config(
        timestep=samples_per_chunk,
        trim_samples=trim_samples,
        mode=mode,
        num_classes=num_classes,
        label_names=["valence", "arousal", "dominance", "liking"],
        shape=shape,
        files=file_dict,
        destination_dir=target_directory
    )


def write_config(
    timestep,
    trim_samples,
    mode,
    num_classes,
    label_names,
    shape,
    files,
    destination_dir
):
    meta_dict = {
        "timestep": timestep,
        "trim_samples": trim_samples,
        "mode": mode,
        "num_classes": num_classes,
        "label_names": label_names,
    }
    json_dict = {
        "meta": meta_dict,
        "shape": shape,
        "files": files
    }

    fn = os.path.join(destination_dir, "meta.json")
    with open(fn, "w") as f:
        json.dump(json_dict, f, indent=4)
    print(f"wrote json config file {fn}")


def unpickle(file):
    with open(file, "rb") as rf:
        x = pickle.load(rf, encoding="latin1")
        data = x["data"]
        labels = x["labels"]
        return data, labels


def process_file(
        pickle_file: str,
        writer: DEAPWriter,
        method: str,
        trim_samples: int,
        file_id: int,
        target_splits: int,
        valence_classes: int,
        arousal_classes: int,
        dominance_classes: int,
        liking_classes: int,
        progress_bar,
        axes_order='sensors-first'):
    data, labels = unpickle(pickle_file)
    # it is easier to work with the data in order experiment x time x channel, so we can iterate over the time axis easier
    data = np.swapaxes(data, 1, 2)
    num_samples = data.shape[1]
    num_channels = data.shape[2]
    label_names = ["valence", "arousal", "dominance", "liking"]
    label_transformer = LabelTransformer(
        valence_classes,
        arousal_classes,
        dominance_classes,
        liking_classes,
        label_names)

    if trim_samples > 0:
        data = data[:, trim_samples:, :]

    num_experiments = data.shape[0]
    if method == "own":
        np.random.shuffle(data)
        for experiment in range(num_experiments):
            experiment_data = data[experiment, :]
            experiment_labels = labels[experiment, :]

            transformed_labels = label_transformer.transform_labels(
                experiment_labels)

            split_experiment_data = np.split(experiment_data,
                                             target_splits,
                                             axis=0)
            random.shuffle(split_experiment_data)

            for sample in split_experiment_data:
                if axes_order == 'sensors-first':
                    sample = np.swapaxes(sample, 0, 1)
                writer.write_example(sample, transformed_labels)
    elif method == "tripathi":
        statfuns = [np.mean,
                    np.median,
                    np.max,
                    np.min,
                    np.std,
                    np.var,
                    np.ptp,
                    scipy.stats.skew,
                    scipy.stats.kurtosis]

        def _run_statistics(input_array, statistics):
            return np.array([fun(input_array) for fun in statistics])

        overall = np.apply_along_axis(
            _run_statistics, 1, data, statistics=statfuns)
        progress_bar.write("Calc'd overall stats")

        slices = np.split(data, 10, axis=1)
        slice_stat = [np.apply_along_axis(_run_statistics,
                                          1,
                                          slice,
                                          statistics=statfuns) for slice in slices]
        progress_bar.write("Calc'd slice-wise stats")

        experiment = np.arange(0, num_experiments, 1).reshape(
            (num_experiments, 1))
        experiment = np.repeat(
            experiment[:, :, np.newaxis], num_channels, axis=2)
        subject = np.ones((num_experiments, 1, num_channels)) * file_id - 1

        concat = np.concatenate(
            slice_stat + [overall, experiment, subject], axis=1)

        np.random.shuffle(concat)

        for experiment in range(num_experiments):
            experiment_labels = labels[experiment, :]
            transformed_labels = label_transformer.transform_labels(
                experiment_labels)
            writer.write_example(concat[experiment, :], transformed_labels)

    else:
        raise ValueError("Unsupported method")


def process_file_subject_dependent(in_file,
                                   target_directory,
                                   train_writer,
                                   test_writer,
                                   current_sample,
                                   validation_indices,
                                   target_windows,
                                   trim_samples,
                                   num_classes,
                                   data_frequency=128,
                                   axes_order="sensors-first",
                                   processing=None):

    data, labels = unpickle(in_file)
    data = np.swapaxes(data, 1, 2)

    valence_classes, arousal_classes, dominance_classes, liking_classes = num_classes
    label_names = ["valence", "arousal", "dominance", "liking"]
    label_transformer = LabelTransformer(
        valence_classes,
        arousal_classes,
        dominance_classes,
        liking_classes,
        label_names)

    if trim_samples > 0:
        data = data[:, trim_samples:, :]

    num_samples_in_experiment = data.shape[1]

    num_experiments = data.shape[0]

    for experiment in range(num_experiments):
        experiment_data = data[experiment, :]
        if processing is not None:
            experiment_data = experiment_data[:, :32]
        experiment_labels = labels[experiment]

        # experiment_data = normalize(experiment_data)

        transformed_labels = label_transformer.transform_labels(
            experiment_labels, return_np=True)
        # split_experiment_data = np.split(
        #     experiment_data,
        #     target_splits,
        #     axis=0)
        split_experiment_data = split_with_windows(
            experiment_data, target_windows)

        if processing == 'wavelet':
            wavelet_data = [dwt_alpha(se, 'db4')
                            for se in split_experiment_data]
            split_experiment_data = wavelet_data

        if processing == 'fft':
            swapped_data = [np.swapaxes(a, 0, 1)
                            for a in split_experiment_data]
            fft_data = [fft(se) for se in swapped_data]
            split_experiment_data = fft_data

        for sample in split_experiment_data:
            if axes_order == "sensors-first" and processing != 'fft':
                sample = np.swapaxes(sample, 0, 1)
            if int(current_sample) in validation_indices:
                test_writer.write_example(sample, transformed_labels)
            else:
                train_writer.write_example(sample, transformed_labels)
            current_sample += 1


def split_with_windows(data, windows):
    def _s(d, w):
        return data[w, :]
    return [_s(data, w) for w in windows]


def fft(x):
    f, t, Sxx = signal.spectrogram(x, fs=128, nperseg=32, axis=1)
    return Sxx


def normalize(data):
    maximum = np.max(data, axis=0)
    minimum = np.min(data, axis=0)
    remap_data = (data-minimum) / (maximum - minimum)
    return remap_data


def dwt_alpha(data, wavelet='db4'):
    _, _, _, _, cAlpha = pywt.wavedec(data, wavelet, level=4, axis=0)
    return cAlpha


if __name__ == "__main__":
    num_subjects = 32
    target_seconds = 1.0
    step = 0.5
    trim_samples = 384  # 384
    input_dir = r'/data/deap/data_preprocessed_python'

    # modes = ["subject-dependent", "subject-independent"]
    modes = ["subject-dependent"]

    for mode in modes:

        print(end="\n"*3)
        print(f"########## {mode} ##########")
        print()

        target_directory = f'/data/deap/all_shuffled/{target_seconds}s{step}/{mode}'
        if mode == 'subject-dependent':
            out_names = [
                os.path.join(target_directory,
                             f"{mode}_{x}.tfrecords") for x in ['train', 'test']]
        elif mode == 'subject-independent':
            file_tag = 'subject'
            file_numbers = [f"{(sp + 1):02}" for sp in range(num_subjects)]
            out_names = [os.path.join(
                target_directory, f'deap_{mode}_{file_tag}{n}.tfrecords') for n in file_numbers]

        if not os.path.isdir(target_directory):
            os.makedirs(target_directory)
            print(f"created {target_directory}")

        process_directory(
            input_dir=input_dir,
            target_directory=target_directory,
            out_names=out_names,
            mode=mode,
            target_seconds=target_seconds,
            trim_samples=trim_samples,
            processing=None,
            window_step=step,
            validation_split=(1/3)
        )
