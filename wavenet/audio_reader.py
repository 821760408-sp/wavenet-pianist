import fnmatch
import os
import random
import re
import threading

import librosa
import numpy as np
import tensorflow as tf

FILE_PATTERN = r'p([0-9]+)_([0-9]+)\.wav'


def get_category_cardinality(files):
    id_reg_expression = re.compile(FILE_PATTERN)
    min_id = None
    max_id = None
    for filename in files:
        matches = id_reg_expression.findall(filename)[0]
        pianist_id, recording_id = [int(id_) for id_ in matches]
        if min_id is None or pianist_id < min_id:
            min_id = pianist_id
        if max_id is None or pianist_id > max_id:
            max_id = pianist_id

    return min_id, max_id


def find_files(directory, pattern='*.wav'):
    """Recursively finds all files matching the pattern."""
    files = []
    for root, dirnames, filenames in os.walk(directory):
        for filename in fnmatch.filter(filenames, pattern):
            files.append(os.path.join(root, filename))
    return files


def load_generic_audio(directory, sample_rate):
    """Generator that yields audio waveforms from the directory."""
    def randomize_files(fns):
        for _ in fns:
            file_index = random.randint(0, len(fns) - 1)
            yield fns[file_index]

    files = find_files(directory)
    id_reg_exp = re.compile(FILE_PATTERN)
    print("files length: {}".format(len(files)))
    randomized_files = randomize_files(files)
    for filename in randomized_files:
        ids = id_reg_exp.findall(filename)
        if not ids:
            # The file name does not match the pattern containing ids, so
            # there is no id.
            category_id = None
        else:
            # The file name matches the pattern for containing ids.
            category_id = int(ids[0][0])
        audio, _ = librosa.load(filename, sr=sample_rate, mono=True)
        # normalize audio
        audio = librosa.util.normalize(audio)
        # trim the last 5 seconds to account for music rollout
        audio = audio[:-5*sample_rate]
        yield audio, filename, category_id


def trim_silence(audio, threshold, frame_length=2048):
    """Removes silence at the beginning and end of a sample."""
    if audio.size < frame_length:
        frame_length = audio.size
    try:
        energy = librosa.feature.rmse(audio, frame_length=frame_length)
    except TypeError:
        energy = librosa.feature.rmse(audio, n_fft=frame_length)
    frames = np.nonzero(energy > threshold)
    indices = librosa.core.frames_to_samples(frames)[1]

    # Note: indices can be an empty array, if the whole audio was silence.
    return audio[indices[0]:indices[-1]] if indices.size else audio[0:0]


def not_all_have_id(files):
    """ Return true iff any of the filenames does not conform to the pattern
        we require for determining the category id."""
    id_reg_exp = re.compile(FILE_PATTERN)
    for f in files:
        ids = id_reg_exp.findall(f)
        if not ids:
            return True
    return False


class AudioReader(object):
    """Generic background audio reader that preprocesses audio files
    and enqueues them into a TensorFlow queue."""
    def __init__(self,
                 audio_dir,
                 coord,
                 sample_rate,
                 gc_enabled,
                 lc_enabled,
                 receptive_field,
                 sample_size=None,
                 silence_threshold=None,
                 queue_size=32):
        self.audio_dir = audio_dir
        self.sample_rate = sample_rate
        self.coord = coord
        self.sample_size = sample_size
        self.receptive_field = receptive_field
        self.silence_threshold = silence_threshold
        self.gc_enabled = gc_enabled
        self.lc_enabled = lc_enabled
        self.threads = []
        self.sample_placeholder = tf.placeholder(dtype=tf.float32, shape=None)
        self.queue = tf.PaddingFIFOQueue(queue_size,
                                         ['float32'],
                                         shapes=[(None, 1)])
        self.enqueue = self.queue.enqueue([self.sample_placeholder])

        if self.gc_enabled:
            self.id_placeholder = tf.placeholder(dtype=tf.int32, shape=())
            self.gc_queue = tf.PaddingFIFOQueue(queue_size, ['int32'],
                                                shapes=[()])
            self.gc_enqueue = self.gc_queue.enqueue([self.id_placeholder])

        if self.lc_enabled:
            self.lc_channels = 1025
            self.lc_embedding_placeholder = tf.placeholder(dtype=tf.float32, shape=(None, self.lc_channels))
            self.lc_queue = tf.PaddingFIFOQueue(queue_size, ['float32'], shapes=[(None, self.lc_channels)])
            self.lc_enqueue = self.lc_queue.enqueue([self.lc_embedding_placeholder])

        # TODO Find a better way to check this.
        # Checking inside the AudioReader's thread makes it hard to terminate
        # the execution of the script, so we do it in the constructor for now.
        files = find_files(audio_dir)
        if not files:
            raise ValueError("No audio files found in '{}'.".format(audio_dir))
        if self.gc_enabled and not_all_have_id(files):
            raise ValueError("Global conditioning is enabled, but file names "
                             "do not conform to pattern having id.")
        # Determine the number of mutually-exclusive categories we will
        # accomodate in our embedding table.
        if self.gc_enabled:
            _, self.gc_category_cardinality = get_category_cardinality(files)
            # Add one to the largest index to get the number of categories,
            # since tf.nn.embedding_lookup expects zero-indexing. This
            # means one or more at the bottom correspond to unused entries
            # in the embedding lookup table. But that's a small waste of memory
            # to keep the code simpler, and preserves correspondance between
            # the id one specifies when generating, and the ids in the
            # file names.
            self.gc_category_cardinality += 1
            print("Detected --gc_cardinality={}".format(
                  self.gc_category_cardinality))
        else:
            self.gc_category_cardinality = None

    def dequeue(self, num_elements):
        output = self.queue.dequeue_many(num_elements)
        return output

    def dequeue_gc(self, num_elements):
        return self.gc_queue.dequeue_many(num_elements)

    def dequeue_lc(self, num_elements):
        return self.lc_queue.dequeue_many(num_elements)

    def thread_main(self, sess):
        stop = False
        # Go through the dataset multiple times
        while not stop:
            iterator = load_generic_audio(self.audio_dir, self.sample_rate)
            for audio, filename, category_id in iterator:
                if self.coord.should_stop():
                    stop = True
                    break
                if self.silence_threshold is not None:
                    # Remove silence
                    audio = trim_silence(audio[:, 0], self.silence_threshold)
                    audio = audio.reshape(-1, 1)
                    if audio.size == 0:
                        print("Warning: {} was ignored as it contains only "
                              "silence. Consider decreasing trim_silence "
                              "threshold, or adjust volume of the audio."
                              .format(filename))

                audio = np.pad(audio, [[self.receptive_field, 0], [0, 0]], 'constant')

                if self.sample_size:
                    # Cut samples into pieces of size receptive_field +
                    # sample_size with receptive_field overlap
                    window = self.receptive_field + self.sample_size
                    while len(audio) > window:
                        piece = audio[:window, :]
                        sess.run(self.enqueue, feed_dict={self.sample_placeholder: piece})
                        audio = audio[self.sample_size:, :]
                        if self.gc_enabled:
                            sess.run(self.gc_enqueue, feed_dict={self.id_placeholder: category_id})
                        if self.lc_enabled:
                            # DEV -- start
                            piece = np.reshape(piece, (piece.shape[0],))  # so that we can run librosa.piptrack()
                            pitches, magnitudes = librosa.piptrack(piece)  # magnitudes here would have shape like
                            # for example (1025, 13975) where 13975 is the number of frames in `piece`
                            # hence we need to transpose `magnitudes` then "upsample" it.
                            magnitudes = np.transpose(magnitudes)
                            lc_embedding = np.zeros(magnitudes.shape, dtype=np.float32)
                            for i in range(magnitudes.shape[0]):
                                nonzero_cnt = len(np.nonzero(magnitudes[i])[0])
                                num_ind_to_keep = min(nonzero_cnt, 6)  # keeping a maximum of 6 detected pitches
                                ind_of_largest_mags = \
                                    np.argpartition(magnitudes[i], -num_ind_to_keep)[-num_ind_to_keep:] \
                                    if num_ind_to_keep != 0 \
                                    else []
                                np.put(lc_embedding[i], ind_of_largest_mags, [1.0])
                            # the upsampling is done in model::loss
                            # DEV -- end
                            sess.run(self.lc_enqueue, feed_dict={self.lc_embedding_placeholder: lc_embedding})
                else:
                    sess.run(self.enqueue,
                             feed_dict={self.sample_placeholder: audio})
                    if self.gc_enabled:
                        sess.run(self.gc_enqueue,
                                 feed_dict={self.id_placeholder: category_id})

    def start_threads(self, sess, n_threads=1):
        for _ in range(n_threads):
            thread = threading.Thread(target=self.thread_main, args=(sess,))
            thread.daemon = True  # Thread will close when parent quits.
            thread.start()
            self.threads.append(thread)
        return self.threads
