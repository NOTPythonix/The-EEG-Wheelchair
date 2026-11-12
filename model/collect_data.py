# Code adapted from NeuroPawn: https://github.com/NeuroPawn/motor-imagery/blob/main/data_collect.py
# File adapted by OpenChamber (DeepSeek V4.1 Flash 1017)

import os, time, random
import numpy as np
from collections import defaultdict
from brainflow.board_shim import BoardShim, BrainFlowInputParams
from scipy.signal import butter, sosfilt, sosfilt_zi

# ====== USER CONFIG ======
board_id        = 57
serial_port     = "COM3"
PROMPT_COUNTS   = False
TRIAL_LENGTH_SECS = 4.0
TRIALS_PER_CLASS_DEFAULT = {'left': 15, 'right': 15, 'tongue': 15, 'feet': 15}       # higher number of trials increases precision and accuracy of the model (usable detector requires minimum of 5 ea)

# process per live filter step
STEP_SAMPLES = 1

window_sec = TRIAL_LENGTH_SECS

# Output
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
output_dir = os.path.join(PROJECT_ROOT, "data")

# Channel Order (board order must match)
chan_names  = ["CP3", "Fp2", "Fp1", "Cz", "FC6", "FC5", "C1", "C2"]

# ====== LABELS ======
labels = ['left', 'right', 'tongue', 'feet']
label_to_class = {'left': 0, 'right': 1, 'rest': 2, 'tongue': 3, 'feet': 4}

# ====== Helpers ======
# board sampling frequency is not exact
# initial buffer timestamps used to get average

def _ask_int(prompt: str, default: int) -> int:
    try:
        s = input(f"{prompt} [{default}]: ").strip()
        return int(s) if s != "" else default
    except Exception:
        print("  (Using default)")
        return default

def fix_len(x, target):
    # x shape expected: (n_ch, n)
    n = x.shape[1] if x.ndim == 2 else 0
    if n == target:
        return x
    if n > target:
        return x[:, -target:]
    if n == 0:
        return np.zeros((x.shape[0], target), dtype=x.dtype)
    pad = target - n
    return np.pad(x, ((0,0),(pad,0)), mode="edge")

def append_chunk(buf, chunk):
    if buf is None or buf.size == 0:
        out = chunk.copy()
    else:
        out = np.concatenate([buf, chunk], axis=1)
    # cap length to last MAX_KEEP samples
    if out.shape[1] > MAX_KEEP:
        out = out[:, -MAX_KEEP:]
    return out

# ====== Live Filter Bank ======
BANDS = {
    "0.05-5":  (0.05, 5.0),
    "8-30":    (8.0, 30.0),
    "8-12":    (8.0, 12.0),
    "12-16":   (12.0, 16.0),
    "16-20":   (16.0, 20.0),
    "20-26":   (20.0, 26.0),
    "26-30":   (26.0, 30.0),
}
FILTER_ORDER = 4

def design_band_sos(low, high, fs, order=4):
    # protect very-low edge
    low = max(low, 0.001)
    nyq = 0.5 * fs
    wp = [low/nyq, high/nyq]
    return butter(order, wp, btype='bandpass', output='sos')

# ====== Runtime state (filled in by setup_board) ======
board             = None
eeg_channels      = []
sr_decl           = None
ts_ch             = None
n_ch              = 0
samples_per_trial = None
MAX_KEEP          = None
band_sos          = {}
band_zi           = {}

raw_buf           = None                                # (n_ch, T)
filt_bufs         = {name: None for name in BANDS.keys()}   # each (n_ch, T)
total_samples     = 0                                   # processed samples (time axis length)

# ====== Live processing ======

def process_live_chunk(eeg_chunk: np.ndarray):
    global raw_buf, filt_bufs, total_samples
    # append raw
    raw_buf = append_chunk(raw_buf, eeg_chunk)

    # step through in configured STEP_SAMPLES for stateful filtering
    k = eeg_chunk.shape[1]
    start = 0
    while start < k:
        end = min(start + STEP_SAMPLES, k)
        step = eeg_chunk[:, start:end]  # (n_ch, m)
        m = step.shape[1]

        # filter each band, channel-wise with persistent zi
        for bname in BANDS.keys():
            sos = band_sos[bname]
            # accumulate band output for this step
            out_step = np.zeros_like(step)
            for ci in range(n_ch):
                y, band_zi[bname][ci] = sosfilt(sos, step[ci], zi=band_zi[bname][ci])
                out_step[ci] = y
            filt_bufs[bname] = append_chunk(filt_bufs[bname], out_step)

        total_samples += m
        start = end

# Pull-and-process whatever the board has; keep UI responsive
def pump_until(at_least_samples_more: int = 0, max_wait_s: float = 2.0):
    """Process live data until we've added >= at_least_samples_more new samples or timeout."""
    target = total_samples + max(0, at_least_samples_more)
    t0 = time.time()
    while total_samples < target and (time.time() - t0) < max_wait_s:
        # read & clear buffer from device
        new = board.get_board_data()
        if new.size == 0:
            time.sleep(0.002)
            continue
        eeg = new[eeg_channels]
        if eeg.ndim == 1:
            eeg = eeg[:, None]
        process_live_chunk(eeg)

def run_for_seconds(secs: float):
    """Keep processing for 'secs' seconds (approx, driven by device buffer availability)."""
    t_end = time.time() + secs
    while time.time() < t_end:
        new = board.get_board_data()
        if new.size == 0:
            time.sleep(0.002)
            continue
        eeg = new[eeg_channels]
        if eeg.ndim == 1:
            eeg = eeg[:, None]
        process_live_chunk(eeg)

def capture_trial_at_current_index(label: str, end_idx: int):
    """
    Slice the last samples_per_trial samples from the *current* live buffers.
    If you're using a capped/ring buffer, you can't use absolute end_idx.
    """
    buf_len = raw_buf.shape[1]                       # <— use live buffer length
    s1 = buf_len
    s0 = max(0, buf_len - samples_per_trial)
    trial = {
        "label": label,
        "class_type": label_to_class[label],
        "raw_eeg": raw_buf[:, s0:s1].copy()
    }
    per_band = {name: filt_bufs[name][:, s0:s1].copy() for name in BANDS.keys()}
    return trial, per_band

# ====== Board lifecycle ======

def setup_board():
    global board, eeg_channels, sr_decl, ts_ch, n_ch
    global samples_per_trial, MAX_KEEP, band_sos, band_zi
    global raw_buf, total_samples

    BoardShim.enable_dev_board_logger()
    params = BrainFlowInputParams()
    params.serial_port = serial_port
    board = BoardShim(board_id, params)

    print("🔌 Preparing board session...")
    board.prepare_session()
    board.start_stream()
    time.sleep(3.0)

    # Per-channel configuration you had (chon gains can be adjusted)
    commands = [
        "chon_1_12", "rldadd_1", "chon_2_12", "rldadd_2",
        "chon_3_12", "rldadd_3", "chon_4_12", "rldadd_4",
        "chon_5_12", "rldadd_5", "chon_6_12", "rldadd_6",
        "chon_7_12", "rldadd_7", "chon_8_12", "rldadd_8"
    ]
    for cmd in commands:
        board.config_board(cmd)
        time.sleep(1.5)
    time.sleep(2.0)
    board.get_board_data()  # clear buffer after cfg
    time.sleep(2.0)

    # ====== Sampling rate & channels ======
    sr_decl      = BoardShim.get_sampling_rate(board_id)
    ts_ch        = BoardShim.get_timestamp_channel(board_id)
    eeg_channels = board.get_eeg_channels(board_id)
    n_ch         = len(eeg_channels)

    samples_per_trial = int(round(window_sec * sr_decl))
    MAX_KEEP          = int(round(sr_decl * 5.0))

    # Per-band: sos and per-channel zi states
    band_sos = {name: design_band_sos(lo, hi, sr_decl, FILTER_ORDER) for name,(lo,hi) in BANDS.items()}
    band_zi  = {name: [sosfilt_zi(band_sos[name]) for _ in range(n_ch)] for name in BANDS.keys()}

    # reset buffers in place (never rebind filt_bufs)
    raw_buf = None
    for name in BANDS.keys():
        filt_bufs[name] = None
    total_samples = 0


def close_board():
    global board
    if board is None:
        return
    try:
        board.stop_stream()
        board.release_session()
        print("🧠 Board session ended.")
    finally:
        board = None

# ====== Active trial quotas ======

def build_quotas():
    trials_per_class_map = dict(TRIALS_PER_CLASS_DEFAULT)
    if PROMPT_COUNTS:
        print("\n=== Per-class trial counts ===")
        for lbl in labels:
            trials_per_class_map[lbl] = _ask_int(f"Number of {lbl.upper()} trials", trials_per_class_map[lbl])
        print("==============================\n")
    for k, v in trials_per_class_map.items():
        if v <= 0:
            raise ValueError(f"Trial count for '{k}' must be > 0 (got {v}).")
    return trials_per_class_map

# ====== Acquisition logic ======

def collect_trials(trials_per_class_map):
    remaining = {lbl: trials_per_class_map[lbl] for lbl in labels}
    done      = {lbl: 0 for lbl in labels}
    trials_meta = []     # list of dicts with label/class/raw
    trials_band = []     # list of dicts with per-band arrays

    # Warm-up a little so we have enough history before first trial
    pump_until(at_least_samples_more=int(2.0 * sr_decl), max_wait_s=3.0)

    print(f"\nPlanned active trials by class: {trials_per_class_map}")
    print(f"Total active trials: {sum(remaining.values())}")
    print("Randomized schedule with ~3-second cadence per trial.")
    time.sleep(2)

    while sum(remaining.values()) > 0:
        choices = [lbl for lbl in labels if remaining[lbl] > 0]
        lbl = random.choice(choices)

        # Countdown while we keep pumping live data
        print(f"\n➡️  Get ready: {lbl.upper()} — in 4…"); run_for_seconds(1.0)
        print("3…"); run_for_seconds(1.0)
        print("2…"); run_for_seconds(1.0)

        # Mark the CLENCH moment by current processed index (end_idx)
        print(f"1 ✊ CLENCH (taking last {window_sec} s from live filtered stream)…"); run_for_seconds(window_sec)
        # Make sure we have at least samples_per_trial in the buffers
        pump_until(at_least_samples_more=max(0, samples_per_trial - total_samples), max_wait_s=2.0)
        end_idx = total_samples  # sample index at the instant of prompt
        # If too early (very first moments), wait a touch more
        if end_idx < samples_per_trial:
            pump_until(at_least_samples_more=(samples_per_trial - end_idx), max_wait_s=2.0)
            end_idx = total_samples

        trial, per_band = capture_trial_at_current_index(lbl, end_idx)
        trials_meta.append(trial)
        trials_band.append(per_band)

        remaining[lbl] -= 1
        done[lbl]      += 1
        tgt = trials_per_class_map
        prog = "Progress — " + " | ".join(f"{lbl.capitalize()} {done[lbl]}/{tgt[lbl]}" for lbl in labels) # Line of code from ChatGPT
        print("✔️  Saved:", lbl.upper())
        print(prog)

    # ====== REST at the end (continuous, then take window_sec windows sequentially) ======
    rest_trials = sum(trials_per_class_map.values())
    print(f"\n😴 Now collecting REST as {rest_trials} consecutive {window_sec} s windows…")

    for j in range(rest_trials):
        # Stream for window_sec, then slice the last window_sec window
        run_for_seconds(window_sec)
        trial, per_band = capture_trial_at_current_index("rest", end_idx=None)  # end_idx unused inside
        trials_meta.append(trial)
        trials_band.append(per_band)
        if (j+1) % 1 == 0 or j == rest_trials-1:
            print(f"   REST windows: {j+1}/{rest_trials}")

    return trials_meta, trials_band

# ====== PACK & SAVE (format unchanged) ======

def save_data(trials_meta, trials_band):
    os.makedirs(output_dir, exist_ok=True)

    filtered_data = {
        "labels": [], "class_types": [],
        **{k: [] for k in BANDS.keys()}
    }
    raw_data = {"labels": [], "class_types": [], "raw_data": []}

    for t_meta, t_band in zip(trials_meta, trials_band):
        label = t_meta["label"]; class_type = label_to_class[label]
        raw   = fix_len(t_meta["raw_eeg"], samples_per_trial)

        filtered_data["labels"].append(label)
        filtered_data["class_types"].append(class_type)
        for k in BANDS.keys():
            filtered_data[k].append(fix_len(t_band[k], samples_per_trial))

        raw_data["labels"].append(label)
        raw_data["class_types"].append(class_type)
        raw_data["raw_data"].append(raw)

    # Stack to arrays
    filtered_data["labels"]      = np.array(filtered_data["labels"])
    filtered_data["class_types"] = np.array(filtered_data["class_types"])
    for k in BANDS.keys():
        filtered_data[k] = np.stack(filtered_data[k], axis=0)   # (n_trials, 8, N)

    raw_data["labels"]      = np.array(raw_data["labels"])
    raw_data["class_types"] = np.array(raw_data["class_types"])
    raw_data["raw_data"]    = np.stack(raw_data["raw_data"], axis=0)  # (n_trials, 8, N)
    # Save
    filename1 = os.path.join(output_dir, "four_class_clench_trials_from_pause.npz")
    np.savez(
        filename1,
        **filtered_data,
        sr_decl=np.int32(sr_decl),
        sr_ts=np.float32(sr_decl),
        window_sec=float(window_sec),
        samples_per_trial=np.int32(samples_per_trial)
    )
    print(f"✅ Saved filtered EEG data to: {filename1}")

    filename2 = os.path.join(output_dir, "raw_four_class_clench_trials_from_pause.npz")
    np.savez(filename2, **raw_data)
    print(f"✅ Saved raw EEG data to: {filename2}")

    return filename1, filename2


def main():
    setup_board()
    try:
        print("EEG channel indices:", eeg_channels)
        print("Assumed names:", chan_names)
        print(f"Declared SR = {sr_decl} Hz | samples_per_trial = {samples_per_trial}")
        print(f"Live step size (per filter update) = {STEP_SAMPLES} samples")

        quotas = build_quotas()
        trials_meta, trials_band = collect_trials(quotas)
    finally:
        close_board()

    save_data(trials_meta, trials_band)


if __name__ == "__main__":
    main()
