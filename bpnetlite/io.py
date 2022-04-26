# io.py
# Author: Jacob Schreiber
# Code adapted from Avanti Shrikumar and Ziga Avsec

import numpy
import torch
import pandas

import pyfaidx
import pyBigWig

from tqdm import tqdm

def one_hot_encode(sequence, ignore='N', alphabet=None, dtype='int8', 
	desc=None, verbose=False, **kwargs):
	"""Converts a string or list of characters into a one-hot encoding.

	This function will take in either a string or a list and convert it into a
	one-hot encoding. If the input is a string, each character is assumed to be
	a different symbol, e.g. 'ACGT' is assumed to be a sequence of four 
	characters. If the input is a list, the elements can be any size.

	Although this function will be used here primarily to convert nucleotide
	sequences into one-hot encoding with an alphabet of size 4, in principle
	this function can be used for any types of sequences.

	Parameters
	----------
	sequence : str or list
		The sequence to convert to a one-hot encoding.

	ignore : str, optional
		A character to indicate setting nothing to 1 for that row, keeping the
		encoding entirely 0's for that row. In the context of genomics, this is
		the N character. Default is 'N'.

	alphabet : set or tuple or list, optional
		A pre-defined alphabet. If None is passed in, the alphabet will be
		determined from the sequence, but this may be time consuming for
		large sequences. Default is None.

	dtype : str or numpy.dtype, optional
		The data type of the returned encoding. Default is int8.

	desc : str or None, optional
		The title to display in the progress bar.

	verbose : bool or str, optional
		Whether to display a progress bar. If a string is passed in, use as the
		name of the progressbar. Default is False.

	kwargs : arguments
		Arguments to be passed into tqdm. Default is None.

	Returns
	-------
	ohe : numpy.ndarray
		A binary matrix of shape (alphabet_size, sequence_length) where
		alphabet_size is the number of unique elements in the sequence and
		sequence_length is the length of the input sequence.
	"""

	d = verbose is False

	if isinstance(sequence, str):
		sequence = list(sequence)

	alphabet = alphabet or numpy.unique(sequence)
	alphabet = [char for char in alphabet if char != ignore]
	alphabet_lookup = {char: i for i, char in enumerate(alphabet)}

	ohe = numpy.zeros((len(sequence), len(alphabet)), dtype=dtype)
	for i, char in tqdm(enumerate(sequence), disable=d, desc=desc, **kwargs):
		if char != ignore:
			idx = alphabet_lookup[char]
			ohe[i, idx] = 1

	return ohe

class DataGenerator(torch.utils.data.Dataset):
	"""A data generator for BPNet inputs.

	This generator takes in an extracted set of sequences, output signals,
	and control signals, and will return a single element with random
	jitter and reverse-complement augmentation applied. Jitter is implemented
	efficiently by taking in data that is wider than the in/out windows by
	two times the maximum jitter and windows are extracted from that.
	Essentially, if an input window is 1000 and the maximum jitter is 128, one
	would pass in data with a length of 1256 and a length 1000 window would be
	extracted starting between position 0 and 256. This  generator must be 
	wrapped by a PyTorch generator object.

	Parameters
	----------
	sequences: torch.tensor, shape=(n, 4, in_window+2*max_jitter)
		A one-hot encoded tensor of `n` example sequences, each of input 
		length `in_window`. See description above for connection with jitter.

	signals: torch.tensor, shape=(n, t, out_window+2*max_jitter)
		The signals to predict, usually counts, for `n` examples with
		`t` output tasks (usually 2 if stranded, 1 otherwise), each of 
		output length `out_window`. See description above for connection 
		with jitter.

	controls: torch.tensor, shape=(n, t, out_window+2*max_jitter) or None, optional
		The control signal to take as input, usually counts, for `n`
		examples with `t` strands and output length `out_window`. If
		None, does not return controls.

	in_window: int, optional
		The input window size. Default is 2114.

	out_window: int, optional
		The output window size. Default is 1000.

	max_jitter: int, optional
		The maximum amount of jitter to add, in either direction, to the
		midpoints that are passed in. Default is 128.

	reverse_complement: bool, optional
		Whether to reverse complement-augment half of the data. Default is True.

	random_state: int or None, optional
		Whether to use a deterministic seed or not.
	"""

	def __init__(self, sequences, signals, controls=None, in_window=2114, 
		out_window=1000, max_jitter=128, reverse_complement=True, 
		random_state=None):
		self.in_window = in_window
		self.out_window = out_window
		self.max_jitter = max_jitter
		
		self.reverse_complement = reverse_complement
		self.random_state = numpy.random.RandomState(random_state)

		self.signals = signals
		self.controls = controls
		self.sequences = sequences	

	def __len__(self):
		return len(self.sequences)

	def __getitem__(self, idx):
		i = self.random_state.choice(len(self.sequences))
		j = self.random_state.randint(self.max_jitter*2)

		X = self.sequences[i][:, j:j+self.in_window]
		y = self.signals[i][:, j:j+self.out_window]

		if self.controls is not None:
			X_ctl = self.controls[i][:, j:j+self.in_window]

		if self.reverse_complement and self.random_state.choice(2) == 1:
			X = X[::-1][:, ::-1]
			y = y[::-1][:, ::-1]

			if self.controls is not None:
				X_ctl = X_ctl[::-1][:, ::-1]

		X = torch.tensor(X.copy(), dtype=torch.float32)
		y = torch.tensor(y.copy())

		if self.controls is not None:
			X_ctl = torch.tensor(X_ctl.copy(), dtype=torch.float32)
			return X, X_ctl, y

		return X, y

def extract_peaks(peaks, sequences, signals, controls=None, chroms=None, 
	in_window=2114, out_window=1000, max_jitter=128, verbose=False):
	"""Extract sequences and signals at coordinates from a peak file.

	This function will take in genome-wide sequences, signals, and optionally
	controls, and extract the values of each at the coordinates specified in
	the peak file and return them as tensors.

	Signals and controls are both lists with the length of the list, n_s
	and n_c respectively, being the middle dimension of the returned
	tensors. Specifically, the returned tensors of size 
	(len(peaks), n_s/n_c, (out_window/in_wndow)+max_jitter*2).

	The values for sequences, signals, and controls, can either be filepaths
	or dictionaries of numpy arrays or a mix of the two. When a filepath is 
	passed in it is loaded using pyfaidx or pyBigWig respectively.   

	Parameters
	----------
	peaks: str or pandas.DataFrame
		Either the path to a bed file or a pandas DataFrame object containing
		three columns: the chromosome, the start, and the end, of each peak.

	sequences: str or dictionary
		Either the path to a fasta file to read from or a dictionary where the
		keys are the unique set of chromosoms and the values are one-hot
		encoded sequences as numpy arrays or memory maps.

	signals: list of strs or list of dictionaries
		A list of filepaths to bigwig files, where each filepath will be read
		using pyBigWig, or a list of dictionaries where the keys are the same
		set of unique chromosomes and the values are numpy arrays or memory
		maps.

	controls: list of strs or list of dictionaries or None, optional
		A list of filepaths to bigwig files, where each filepath will be read
		using pyBigWig, or a list of dictionaries where the keys are the same
		set of unique chromosomes and the values are numpy arrays or memory
		maps. If None, no control tensor is returned. Default is None. 

	chroms: list or None, optional
		A set of chromosomes to extact peaks from. Peaks in other chromosomes
		in the peak file are ignored. If None, all peaks are used. Default is
		None.

	in_window: int, optional
		The input window size. Default is 2114.

	out_window: int, optional
		The output window size. Default is 1000.

	max_jitter: int, optional
		The maximum amount of jitter to add, in either direction, to the
		midpoints that are passed in. Default is 128.

	verbose: bool, optional
		Whether to display a progress bar while loading. Default is False.

	Returns
	-------
	seqs: numpy.ndarray, shape=(n, 4, in_window+2*max_jitter)
		The extracted sequences in the same order as the peaks in the peak
		file after optional filtering by chromosome.

	signals: numpy.ndarray, shape=(n, len(signals), out_window+2*max_jitter)
		The extracted signals where the first dimension is in the same order
		as peaks in the peak file after optional filtering by chromosome and
		the second dimension is in the same order as the list of signal files.

	controls: numpy.ndarray, shape=(n, len(controls), out_window+2*max_jitter)
		The extracted controls where the first dimension is in the same order
		as peaks in the peak file after optional filtering by chromosome and
		the second dimension is in the same order as the list of control files.
		If no control files are given, this is not returned.
	"""

	seqs, signals_, controls_ = [], [], []
	in_width, out_width = in_window // 2, out_window // 2

	# Load the sequences
	if isinstance(sequences, str):
		sequences = pyfaidx.Fasta(sequences)

	# Load the peaks or rename the columns to be consistent
	names = ['chrom', 'start', 'end']
	if isinstance(peaks, str):
		peaks = pandas.read_csv(peaks, sep="\t", usecols=(0, 1, 2), 
			header=None, index_col=False, names=names)
	else:
		peaks = peaks.copy()
		peaks.columns = names

	if chroms is not None:
		peaks = peaks[numpy.isin(peaks['chrom'], chroms)]

	# Load the signal and optional control tracks if filenames are given
	for i, signal in enumerate(signals):
		if isinstance(signal, str):
			signals[i] = pyBigWig.open(signal, "r")

	if controls is not None:
		for i, control in enumerate(controls):
			if isinstance(control, str):
				controls[i] = pyBigWig.open(control, "r")

	desc = "Loading Peaks"
	d = not verbose
	for _, (chrom, start, end) in tqdm(peaks.iterrows(), disable=d, desc=desc):
		mid = start + (end - start) // 2
		start = mid - out_width - max_jitter
		end = mid + out_width + max_jitter

		# Extract the signal from each of the signal files
		signals_.append([])
		for signal in signals:
			if isinstance(signal, dict):
				signal_ = signal[chrom][start:end]
			else:
				signal_ = signal.values(chrom, start, end, numpy=True)
				signal_ = numpy.nan_to_num(signal_)

			signals_[-1].append(signal_)

		# For the sequences and controls extract a window the size of the input
		start = mid - in_width - max_jitter
		end = mid + in_width + max_jitter

		# Extract the controls from each of the control files
		if controls is not None:
			controls_.append([])
			for control in controls:
				if isinstance(control, dict):
					control_ = control[chrom][start:end]
				else:
					control_ = control.values(chrom, start, end, numpy=True)
					control_ = numpy.nan_to_num(control_)

				controls_[-1].append(control_)

		# Extract the sequence
		if isinstance(sequences, dict):
			seq = sequences[chrom][start:end].T
		else:
			seq = one_hot_encode(sequences[chrom][start:end].seq.upper(), 
				alphabet=['A', 'C', 'G', 'T', 'N']).T
		
		seqs.append(seq)

	seqs = numpy.array(seqs)
	signals_ = numpy.array(signals_)

	if controls is not None:
		controls_ = numpy.array(controls_)
		return seqs, signals_, controls_

	return seqs, signals_

def PeakGenerator(peaks, sequences, signals, controls=None, chroms=None, 
	in_window=2114, out_window=1000, max_jitter=128, reverse_complement=True, 
	random_state=None, pin_memory=True, num_workers=0, batch_size=32, 
	verbose=False):
	"""This is a constructor function that handles all IO.

	This function will extract signal from all signal and control files,
	pass that into a DataGenerator, and wrap that using a PyTorch data
	loader. This is the only function that needs to be used.

	Parameters
	----------
	peaks: str or pandas.DataFrame
		Either the path to a bed file or a pandas DataFrame object containing
		three columns: the chromosome, the start, and the end, of each peak.

	sequences: str or dictionary
		Either the path to a fasta file to read from or a dictionary where the
		keys are the unique set of chromosoms and the values are one-hot
		encoded sequences as numpy arrays or memory maps.

	signals: list of strs or list of dictionaries
		A list of filepaths to bigwig files, where each filepath will be read
		using pyBigWig, or a list of dictionaries where the keys are the same
		set of unique chromosomes and the values are numpy arrays or memory
		maps.

	controls: list of strs or list of dictionaries or None, optional
		A list of filepaths to bigwig files, where each filepath will be read
		using pyBigWig, or a list of dictionaries where the keys are the same
		set of unique chromosomes and the values are numpy arrays or memory
		maps. If None, no control tensor is returned. Default is None. 

	chroms: list or None, optional
		A set of chromosomes to extact peaks from. Peaks in other chromosomes
		in the peak file are ignored. If None, all peaks are used. Default is
		None.

	in_window: int, optional
		The input window size. Default is 2114.

	out_window: int, optional
		The output window size. Default is 1000.

	max_jitter: int, optional
		The maximum amount of jitter to add, in either direction, to the
		midpoints that are passed in. Default is 128.

	reverse_complement: bool, optional
		Whether to reverse complement-augment half of the data. Default is True.

	random_state: int or None, optional
		Whether to use a deterministic seed or not.

	pin_memory: bool, optional
		Whether to pin page memory to make data loading onto a GPU easier.
		Default is True.

	num_workers: int, optional
		The number of processes fetching data at a time to feed into a model.
		If 0, data is fetched from the main process. Default is 0.

	batch_size: int, optional
		The number of data elements per batch. Default is 32.
	
	verbose: bool, optional
		Whether to display a progress bar while loading. Default is False.

	Returns
	-------
	X: torch.utils.data.DataLoader
		A PyTorch DataLoader wrapped DataGenerator object.
	"""

	X = extract_peaks(peaks=peaks, sequences=sequences, signals=signals, 
		controls=controls, chroms=chroms, in_window=in_window, 
		out_window=out_window, max_jitter=max_jitter, verbose=verbose)

	if controls is not None:
		sequences, signals_, controls_ = X
	else:
		sequences, signals_ = X
		controls_ = None

	X_gen = DataGenerator(sequences, signals_, controls=controls_, 
		in_window=in_window, out_window=out_window, max_jitter=max_jitter,
		reverse_complement=reverse_complement, random_state=random_state)

	X_gen = torch.utils.data.DataLoader(X_gen, pin_memory=pin_memory,
		num_workers=num_workers, batch_size=batch_size) 

	return X_gen