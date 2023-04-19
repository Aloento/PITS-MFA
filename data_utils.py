# modified from https://github.com/jaywalnut310/vits
import os
import random

import torch
import torch.utils.data
from tqdm import tqdm

from analysis import Pitch
from mel_processing import spectrogram_torch
from text import cleaned_text_to_sequence
from utils import load_wav_to_torch, load_filepaths_and_text


class TextAudioLoader(torch.utils.data.Dataset):
  """
      1) loads audio, speaker_id, text pairs
      2) normalizes text and converts them to sequences of integers
      3) computes spectrograms from audio files.
  """

  def __init__(self, audiopaths_sid_text, hparams, pt_run=False):
    self.audiopaths_sid_text = load_filepaths_and_text(audiopaths_sid_text)
    self.sampling_rate = hparams.sampling_rate
    self.filter_length = hparams.filter_length
    self.hop_length = hparams.hop_length
    self.win_length = hparams.win_length

    self.add_blank = hparams.add_blank
    self.min_text_len = getattr(hparams, "min_text_len", 1)
    self.max_text_len = getattr(hparams, "max_text_len", 190)

    self.data_path = hparams.data_path

    self.pitch = Pitch(sr=hparams.sampling_rate,
                       W=hparams.tau_max,
                       tau_max=hparams.tau_max,
                       midi_start=hparams.midi_start,
                       midi_end=hparams.midi_end,
                       octave_range=hparams.octave_range)

    random.seed(1234)
    random.shuffle(self.audiopaths_sid_text)
    self._filter()
    if pt_run:
      for _audiopaths_sid_text in self.audiopaths_sid_text:
        _ = self.get_audio_text_pair(_audiopaths_sid_text,
                                     True)

  def _filter(self):
    """
    Filter text & store spec lengths
    """
    # Store spectrogram lengths for Bucketing
    # wav_length ~= file_size / (wav_channels * Bytes per dim) = file_size / (1 * 2)
    # spec_length = wav_length // hop_length

    audiopaths_sid_text_new = []
    lengths = []
    for id_, phonemes, durations in self.audiopaths_sid_text:
      if self.min_text_len <= len(phonemes) <= self.max_text_len:
        wav_path = os.path.join(self.data_path, id_) + ".wav"
        audiopaths_sid_text_new.append([wav_path, phonemes, durations])
        lengths.append(os.path.getsize(wav_path) // (2 * self.hop_length))

    self.audiopaths_sid_text = audiopaths_sid_text_new
    self.lengths = lengths

  def get_audio_text_pair(self, audiopath_and_text, pt_run=False):
    wav_path, phonemes, durations = audiopath_and_text
    phonemes = self.get_phonemes(phonemes)
    phn_dur = self.get_duration_flag(durations)

    spec, ying, wav = self.get_audio(wav_path, pt_run)

    sumdur = sum(phn_dur)
    assert abs(spec.shape[-1] - sumdur) < 2, wav_path

    if spec.shape[-1] > sumdur:
      spec = spec[:, :sumdur]
      wav = wav[:, :sumdur * self.hop_length]
    elif spec.shape[-1] < sumdur:
      spec_pad = torch.zeros([spec.shape[0], sumdur])
      wav_pad = torch.zeros([1, sumdur * self.hop_length])
      spec_pad[:, :spec.shape[-1]] = spec
      wav_pad[:, :wav.shape[-1]] = wav
      spec = spec_pad
      wav = wav_pad

    assert phonemes.shape == phn_dur.shape, wav_path
    assert sumdur == wav.shape[-1] // self.hop_length

    return phonemes, spec, ying, wav, phn_dur

  def get_audio(self, filename, pt_run=False):
    audio, sampling_rate = load_wav_to_torch(filename)
    if sampling_rate != self.sampling_rate:
      raise ValueError("{} SR doesn't match target {} SR".format(
        sampling_rate, self.sampling_rate))
    audio_norm = audio.unsqueeze(0)
    spec_filename = filename.replace(".wav", ".spec.pt")
    ying_filename = filename.replace(".wav", ".ying.pt")
    if os.path.exists(spec_filename) and not pt_run:
      spec = torch.load(spec_filename, map_location='cpu')
    else:
      spec = spectrogram_torch(audio_norm,
                               self.filter_length,
                               self.sampling_rate,
                               self.hop_length,
                               self.win_length,
                               center=False)
      spec = torch.squeeze(spec, 0)
      torch.save(spec, spec_filename)
    if os.path.exists(ying_filename) and not pt_run:
      ying = torch.load(ying_filename, map_location='cpu')
    else:
      wav = torch.nn.functional.pad(
        audio_norm.unsqueeze(0),
        (self.filter_length - self.hop_length,
         self.filter_length - self.hop_length +
         (-audio_norm.shape[1]) % self.hop_length + self.hop_length * (audio_norm.shape[1] % self.hop_length == 0)),
        mode='constant').squeeze(0)
      ying = self.pitch.yingram(wav)[0]
      torch.save(ying, ying_filename)
    return spec, ying, audio_norm

  def get_phonemes(self, phonemes):
    text_norm = cleaned_text_to_sequence(phonemes.split(" "))
    text_norm = torch.LongTensor(text_norm)
    return text_norm

  def get_duration_flag(self, phn_dur):
    phn_dur = [int(i) for i in phn_dur.split(" ")]
    phn_dur = torch.LongTensor(phn_dur)
    return phn_dur

  def __getitem__(self, index):
    return self.get_audio_text_pair(
      self.audiopaths_sid_text[index])

  def __len__(self):
    return len(self.audiopaths_sid_text)


class TextAudioCollate:
  """ Zero-pads model inputs and targets"""

  def __init__(self, return_ids=False):
    self.return_ids = return_ids

  def __call__(self, batch):
    """Collate's training batch from normalized text, audio and speaker identities
    PARAMS
    ------
    batch: [text_normalized, spec_normalized, wav_normalized, sid]
    """
    # Right zero-pad all one-hot text sequences to max input length
    _, ids_sorted_decreasing = torch.sort(torch.LongTensor(
      [x[1].size(1) for x in batch]),
      dim=0,
      descending=True)

    # phonemes, spec, ying, wav, phn_dur
    max_phonemes_len = max([len(x[0]) for x in batch])
    max_spec_len = max([x[1].size(1) for x in batch])
    max_ying_len = max([x[2].size(1) for x in batch])
    max_wav_len = max([x[3].size(1) for x in batch])
    max_phndur_len = max([len(x[4]) for x in batch])

    phonemes_lengths = torch.LongTensor(len(batch))
    spec_lengths = torch.LongTensor(len(batch))
    ying_lengths = torch.LongTensor(len(batch))
    wav_lengths = torch.LongTensor(len(batch))

    phonemes_padded = torch.LongTensor(len(batch), max_phonemes_len)
    spec_padded = torch.FloatTensor(len(batch), batch[0][1].size(0),
                                    max_spec_len)
    ying_padded = torch.FloatTensor(len(batch), batch[0][2].size(0),
                                    max_ying_len)
    wav_padded = torch.FloatTensor(len(batch), 1, max_wav_len)
    phndur_padded = torch.LongTensor(len(batch), max_phndur_len)

    phonemes_padded.zero_()
    spec_padded.zero_()
    ying_padded.zero_()
    wav_padded.zero_()
    phndur_padded.zero_()

    for i in range(len(ids_sorted_decreasing)):
      row = batch[ids_sorted_decreasing[i]]

      phonemes = row[0]
      phonemes_padded[i, :phonemes.size(0)] = phonemes
      phonemes_lengths[i] = phonemes.size(0)

      spec = row[1]
      spec_padded[i, :, :spec.size(1)] = spec
      spec_lengths[i] = spec.size(1)

      ying = row[2]
      ying_padded[i, :, :ying.size(1)] = ying
      ying_lengths[i] = ying.size(1)

      wav = row[3]
      wav_padded[i, :, :wav.size(1)] = wav
      wav_lengths[i] = wav.size(1)

      phndur = row[4]
      phndur_padded[i, :phndur.size(0)] = phndur

    return phonemes_padded, phonemes_lengths, \
      spec_padded, spec_lengths, \
      ying_padded, ying_lengths, \
      wav_padded, wav_lengths, \
      phndur_padded


class DistributedBucketSampler(torch.utils.data.distributed.DistributedSampler):
  """
  Maintain similar input lengths in a batch.
  Length groups are specified by boundaries.
  Ex) boundaries = [b1, b2, b3] -> any batch is included either {x | b1 < length(x) <=b2} or {x | b2 < length(x) <= b3}.

  It removes samples which are not included in the boundaries.
  Ex) boundaries = [b1, b2, b3] -> any x s.t. length(x) <= b1 or length(x) > b3 are discarded.
  """

  def __init__(self,
               dataset,
               batch_size,
               boundaries,
               num_replicas=None,
               rank=None,
               shuffle=True):
    super().__init__(dataset,
                     num_replicas=num_replicas,
                     rank=rank,
                     shuffle=shuffle)
    self.lengths = dataset.lengths
    self.batch_size = batch_size
    self.boundaries = boundaries

    self.buckets, self.num_samples_per_bucket = self._create_buckets()
    self.total_size = sum(self.num_samples_per_bucket)
    self.num_samples = self.total_size // self.num_replicas

  def _create_buckets(self):
    buckets = [[] for _ in range(len(self.boundaries) - 1)]
    for i in range(len(self.lengths)):
      length = self.lengths[i]
      idx_bucket = self._bisect(length)
      if idx_bucket != -1:
        buckets[idx_bucket].append(i)

    for i in range(len(buckets) - 1, -1, -1):
      if len(buckets[i]) == 0:
        buckets.pop(i)
        self.boundaries.pop(i + 1)

    num_samples_per_bucket = []
    for i in range(len(buckets)):
      len_bucket = len(buckets[i])
      total_batch_size = self.num_replicas * self.batch_size
      rem = (total_batch_size -
             (len_bucket % total_batch_size)) % total_batch_size
      num_samples_per_bucket.append(len_bucket + rem)
    return buckets, num_samples_per_bucket

  def __iter__(self):
    # deterministically shuffle based on epoch
    g = torch.Generator()
    g.manual_seed(self.epoch)

    indices = []
    if self.shuffle:
      for bucket in self.buckets:
        indices.append(
          torch.randperm(len(bucket), generator=g).tolist())
    else:
      for bucket in self.buckets:
        indices.append(list(range(len(bucket))))

    batches = []
    for i in range(len(self.buckets)):
      bucket = self.buckets[i]
      len_bucket = len(bucket)
      ids_bucket = indices[i]
      num_samples_bucket = self.num_samples_per_bucket[i]

      # add extra samples to make it evenly divisible
      rem = num_samples_bucket - len_bucket
      ids_bucket = ids_bucket + ids_bucket * \
                   (rem // len_bucket) + ids_bucket[:(rem % len_bucket)]

      # subsample
      ids_bucket = ids_bucket[self.rank::self.num_replicas]

      # batching
      for j in range(len(ids_bucket) // self.batch_size):
        batch = [
          bucket[idx]
          for idx in ids_bucket[j * self.batch_size:(j + 1) *
                                                    self.batch_size]
        ]
        batches.append(batch)

    if self.shuffle:
      batch_ids = torch.randperm(len(batches), generator=g).tolist()
      batches = [batches[i] for i in batch_ids]
    self.batches = batches

    assert len(self.batches) * self.batch_size == self.num_samples
    return iter(self.batches)

  def _bisect(self, x, lo=0, hi=None):
    if hi is None:
      hi = len(self.boundaries) - 1

    if hi > lo:
      mid = (hi + lo) // 2
      if self.boundaries[mid] < x and x <= self.boundaries[mid + 1]:
        return mid
      elif x <= self.boundaries[mid]:
        return self._bisect(x, lo, mid)
      else:
        return self._bisect(x, mid + 1, hi)
    else:
      return -1

  def __len__(self):
    return self.num_samples // self.batch_size


def create_spec(audiopaths_sid_text, hparams):
  audiopaths_sid_text = load_filepaths_and_text(audiopaths_sid_text)
  for audiopath, _, _ in tqdm(audiopaths_sid_text):
    audiopath = os.path.join(hparams.data_path, audiopath) + ".wav"
    audio, sampling_rate = load_wav_to_torch(audiopath)
    if sampling_rate != hparams.sampling_rate:
      raise ValueError("{} SR doesn't match target {} SR".format(
        sampling_rate, hparams.sampling_rate))
    audio_norm = audio.unsqueeze(0)
    specpath = audiopath.replace(".wav", ".spec.pt")

    if not os.path.exists(specpath):
      spec = spectrogram_torch(audio_norm,
                               hparams.filter_length,
                               hparams.sampling_rate,
                               hparams.hop_length,
                               hparams.win_length,
                               center=False)
      spec = torch.squeeze(spec, 0)
      torch.save(spec, specpath)
