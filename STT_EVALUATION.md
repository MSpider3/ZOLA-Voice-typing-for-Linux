# Speech-to-Text Models for ZOLA: English, Hindi and Hinglish on a Low-End CPU

**Evaluation report, October 2026**

## Abstract

ZOLA targets laptops without a GPU and speakers of English, Hindi and Hinglish (Hindi–English code-switching). Twelve open speech recognition models were tested, all offline on a 2-core Intel i3 laptop CPU, on English, Hindi and Hinglish speech, in clean and noisy conditions.

**Oriserve Whisper-Hindi2Hinglish-Swift**, converted to CTranslate2 int8, gave the best balance by a wide margin:

| | Swift | ZOLA's previous default (Whisper `small`) |
|---|---|---|
| Hindi word error rate | 10% | 44% |
| Hinglish word error rate | 12% | 51% |
| Processing time (RTF, lower is faster) | 0.16 | 1.46 |
| Disk / RAM | 78 MB / 0.9 GB | – |

**NVIDIA Nemotron 3.5** was the most accurate model for English (8%) and the best choice for Hindi written in Devanagari.

The tests also exposed a decoding setting that made the previous pipeline up to 6× slower than real time on hard audio, and showed that small local LLMs are unreliable for cleaning up transcripts.

## 1. Objective

Find which models give usable dictation for English, Hindi and Hinglish on a weak CPU, measured by:

1. accuracy,
2. speed and latency per spoken sentence,
3. memory use, and
4. robustness to noise and real (non-studio) speech.

## 2. Experimental setup

**Hardware.** Intel Core i3-1005G1 (2 cores, 4 threads, Ice Lake), 20 GB RAM, integrated graphics only. All inference ran on the CPU with 2 threads, except the thread-count test.

**Runtimes.**

- faster-whisper / CTranslate2 (int8) for Whisper-family models
- sherpa-onnx (int8 ONNX) for Nemotron, Qwen3-ASR, Moonshine and Omnilingual
- onnx-asr for IndicConformer

**Models tested.**

| Model | Parameters | Runtime | Language coverage |
|---|---|---|---|
| Whisper `small`, `base` (multilingual) | 244M, 74M | faster-whisper int8 | many |
| Whisper `base.en`, `small.en`, distil-whisper `distil-small.en` | 74–244M | faster-whisper int8 | English |
| Oriserve Whisper-Hindi2Hinglish-Swift | 72M | faster-whisper int8 (converted) | Hindi/Hinglish → Roman script |
| Oriserve Whisper-Hindi2Hinglish-Apex | ~800M | faster-whisper int8 (converted) | Hindi/Hinglish → Roman script |
| NVIDIA Nemotron 3.5 ASR streaming | 600M | sherpa-onnx int8 | 40 locales incl. Hindi |
| Qwen3-ASR 0.6B | 600M | sherpa-onnx int8 | 30 languages incl. Hindi |
| AI4Bharat IndicConformer (Hindi) | 120M | onnx-asr | Hindi |
| Meta Omnilingual ASR CTC | 300M | sherpa-onnx int8 | 1,600 languages |
| Moonshine base v2 | 61M | sherpa-onnx | English |

**Test data.**

- **Synthetic speech:** 30 sentences, 10 each in English, Hindi and Hinglish. They cover everyday speech, questions, numbers and phone numbers, dates and festivals, names and places, technical vocabulary, and intra-sentence code-switching. Speech was generated offline with Piper TTS using six voices (three Hindi, three English).
- **Real speech:** a 104-second recording by one native Hindi speaker on a laptop microphone, in three parts: English (36 s), Hindi (35 s) and Hinglish (33 s). The recording is quiet (peaks at about −27 dBFS), which is typical of built-in laptop microphones.
- **Noisy speech:** every clip mixed at 10 dB SNR with background chatter, a 100 Hz fan hum and broadband hiss.

**Metrics.**

- **English:** word error rate (WER) after lowercasing and removing punctuation. Numbers count as correct written as digits or as words.
- **Hindi and Hinglish:** WER and character error rate (CER) after **romanization**. Devanagari output is transliterated with Hindi schwa deletion, and common spelling variants are folded together (aa/a, ee/i, w/v, aspirated/unaspirated, mein/me…). This lets Devanagari, Roman and mixed-script outputs be compared on what was recognised rather than on how it was written.
- **Speed:** real-time factor (RTF) = processing time ÷ audio length; below 1 is faster than real time. Also measured: seconds per sentence and peak process memory.

## 3. Results

### 3.1 Accuracy on clean speech

Word error rate, %; lower is better. "Real" = the speaker's recording.

| Model | English | Hindi | Hinglish | Hindi CER | Hinglish CER | Real EN | Real HI | Real HX |
|---|---|---|---|---|---|---|---|---|
| Whisper `small`, auto language (previous ZOLA default) | 16 | 44 | 51 | 20 | 23 | 10 | 46 | 29 |
| **Hinglish-Swift** | 18 | **10** | **12** | **3** | **5** | 13 | 23 | **13** |
| Hinglish-Apex | 17 | 6 | 8 | 1 | 4 | 10 | 12 | 12 |
| **Nemotron 3.5** (language set by user) | **8** | 17 | 36 | 7 | 16 | **4** | **13** | 45 |
| Nemotron 3.5 (automatic language) | 8 | 17 | 33 | 7 | 15 | 100 | 13 | 45 |
| Qwen3-ASR 0.6B | 6–7 | 24 | 39–40 | 10 | 20–21 | 7 | 17–18 | 10–18 |
| IndicConformer Hindi | – | 17 | 35 | 7 | 16 | – | 54 | 51 |
| Omnilingual 300M | 14 | 34 | 69 | 23 | 46 | 13 | 29 | 93 |
| Whisper `base` (multilingual) | 12 | 97 | 84 | 90 | 71 | 14 | 90 | 57 |
| Whisper `base.en` | 11 | – | – | – | – | 11 | – | – |
| Whisper `small.en` | 12 | – | – | – | – | 8 | – | – |
| distil-whisper `distil-small.en` | 14 | – | – | – | – | 14 | – | – |
| Moonshine base-en v2 | 4 | – | – | – | – | failed | – | – |

### 3.2 Speed and memory

| Model | RTF | Seconds per sentence (3–7 s clips) | Peak RAM |
|---|---|---|---|
| Moonshine base-en v2 | 0.05 | 0.2 | 0.5 GB |
| IndicConformer Hindi | 0.09 | 0.4 | 0.9 GB |
| Whisper `base.en` | 0.12 | 0.9 | 0.9 GB |
| **Hinglish-Swift** | **0.16** | **1.1** | **0.9 GB** |
| Whisper `small.en` | 0.31 | – | 1.5 GB |
| **Nemotron 3.5** | 0.34 | 1.7 | 1.0–1.5 GB |
| Qwen3-ASR 0.6B | 0.76 | 2.9 | 3.2 GB |
| Omnilingual 300M | 0.77 | – | 1.3 GB |
| Whisper `small` (fixed decoding) | 0.88 | 6.0 | 1.4 GB |
| Whisper `small` (previous decoding) | 1.46–2.48 | 5–7; up to 204 s for a 35 s clip | 2.3 GB |
| Hinglish-Apex | 4.79 | – | 2.1 GB |

**Threads.** With 4 threads instead of 2, accuracy was unchanged but both models got slower:

| Model | RTF, 2 threads | RTF, 4 threads |
|---|---|---|
| Hinglish-Swift | 0.16 | 0.20 |
| Nemotron 3.5 | 0.34 | 0.71 |

On this CPU the extra hyperthreads compete with the physical cores.

### 3.3 Noise robustness (10 dB SNR)

| Model | English | Hindi | Hinglish | Real EN | Real HI | Real HX |
|---|---|---|---|---|---|---|
| Hinglish-Swift | 21 | 13 | 18 | 24 | 29 | 46 |
| Nemotron 3.5 | 11 | 19 | 38 | 20 | 28 | 49 |
| Whisper `small` | 9 | 47 | 61 | 4 | 68 | 98 |

### 3.4 Live dictation

The real Hinglish recording was replayed at real speed through ZOLA's realtime pipeline. With Hinglish-Swift:

- Each segment's text was ready about 1.0 s after the segment ended.
- The speaker paused only briefly, so segments were closed by the 10 s length limit; text arrived roughly every 10 s.
- A 6 s limit gave more frequent updates but more errors at the cut points.

## 4. Findings by model

**Hinglish-Swift (Oriserve, Apache-2.0).**

- The best Hindi and Hinglish accuracy among models that run comfortably on this CPU: about 1 s per sentence, 0.9 GB RAM.
- It writes Hindi and Hinglish in Roman script ("Kal office mein meeting hai, please time par aa jaana"), which matches how most users type Hinglish.
- It needs Whisper's English language token for all speech; with the Hindi token it falls back to Devanagari. A fixed language also removes the language-detection pass.
- Weaknesses:
  - English is slightly behind English-only models.
  - Some proper nouns and technical terms are wrong ("NTM install" for "npm install").
  - The romanization is formal ("dekhane ke lie" rather than "dekhne ke liye").
- The int8 conversion is published as [MSpider3/whisper-hindi2hinglish-swift-ct2](https://huggingface.co/MSpider3/whisper-hindi2hinglish-swift-ct2).

**Nemotron 3.5 (NVIDIA).**

- The most accurate English (4% on the real speaker) and good Devanagari Hindi, at about 1.7 s per sentence.
- **Automatic language detection is unreliable for Indian English.** It labelled the real speaker's English as Hindi and wrote it in Devanagari (100% WER), so the spoken language should be set explicitly.
- Hinglish is written entirely in Devanagari, English words included ("दिस विल बी ए मिस बिट्वीन").
- It is a true streaming model, so it could transcribe during speech rather than after.

**Qwen3-ASR 0.6B (Alibaba).**

- The best result on the real Hinglish recording (10–18%). It writes code-switched speech in Latin script and numbers as digits.
- Too heavy for this class of machine: 3.2 GB RAM, and the 35 s Hindi recording took 66 s.
- Once emitted a word in Arabic script.
- Suitable for machines with 4 or more cores.

**Hinglish-Apex (Oriserve).** The most accurate model tested for Hindi and Hinglish, but at RTF 4.8 a 30 s utterance takes about 2.5 minutes. Not usable on low-end CPUs.

**IndicConformer Hindi (AI4Bharat).**

- Very fast, with Hindi accuracy equal to Nemotron on synthetic speech.
- Dropped large parts of the real recording (54%).
- Hindi only, and Devanagari only.

**Whisper `small` and `base` (multilingual).**

- `base` cannot transcribe Hindi (97%).
- `small` is weak for Hindi and Hinglish even after the decoding fix, and on Hinglish it sometimes translated into English or repeated itself.
- English-only Whisper models (`base.en`, `small.en`) are fast and accurate for English.

**Omnilingual 300M (Meta).** Broad language coverage, but weak on Hinglish (69%) and on the real recording (93%).

**Moonshine base-en v2.** The fastest English model, with excellent accuracy on synthetic speech. Its ONNX export fails on some input lengths and returned empty output on the real recording, so it is not yet reliable.

## 5. Findings about the pipeline

**Temperature fallback.**

- Whisper's default decoding retries an utterance up to five times at increasing randomness whenever the output looks unreliable.
- On the real Hindi recording this turned 35 s of audio into 71–204 s of processing and produced random characters.
- Greedy decoding with a repetition guard removed the problem: the same audio took about 4 s with Hinglish-Swift.

**Language detection.** Automatic detection costs a full extra pass of the model per utterance. Fixing the language per model, or detecting with the smaller model, reduces latency.

**Microphone level.** The real recording peaked near −27 dBFS. Normalizing the volume did not change Whisper's errors, but quiet input reduced robustness to noise (section 3.3).

**LLM post-correction.** Two local models were tested for grammar and punctuation cleanup on this CPU:

- **qwen2.5 1.5B:**
  - It answered a dictated question instead of transcribing it.
  - It replaced Hinglish text with different content.
  - It refused to translate.
- **qwen3.5 4B:** better quality, but it took 24–73 s per sentence and once reversed a meaning ("reached home late" became "reached home early").
- **Mitigation:** marking the transcript as data and rejecting outputs that change length or drop words blocks answers and rewrites, but not subtle meaning changes.
- On 2-core machines, LLM cleanup is too slow and too risky to enable by default.

## 6. Recommendations

| Use case | Batch model | Realtime model | Spoken-language setting |
|---|---|---|---|
| Hindi / Hinglish, Roman script | Hinglish-Swift | Hinglish-Swift | any |
| Hindi in Devanagari | Nemotron 3.5 | Nemotron 3.5 | Hindi |
| English, highest accuracy | Nemotron 3.5 | Whisper `base.en` | English |
| English, lowest resource use | Whisper `small.en` | Whisper `base.en` | English |

General settings:

- Use as many threads as the CPU has physical cores.
- Use greedy decoding.
- Keep LLM cleanup off on low-end CPUs.

## 7. Limitations

- The synthetic clips are clean, evenly paced TTS speech; real speech is harder.
- The real-speech results come from one speaker and 104 s of audio, and the reference transcript for it was itself machine-generated.
- Romanized scoring cannot remove every valid spelling variant of Hinglish, so CER is the more reliable Hinglish number.
- All numbers come from one machine; RTF scales roughly with single-core speed and core count.

## 8. Future work

- **Streaming decoding with Nemotron:** feeding audio during recording would make results available almost as soon as the speaker stops.
- **Speaker adaptation:** a LoRA or full fine-tune of Hinglish-Swift on 1–3 hours of one user's dictation is the most promising accuracy gain at the same speed. Training needs a GPU for a few hours.
- **Distillation:** using Hinglish-Apex (6–8% WER) as a teacher to label unlabelled Hindi/Hinglish audio, then training a model of Swift's size on those labels, could bring accuracy closer to Apex at Swift's speed.
- **Training from scratch:** not recommended. It would need thousands of hours of transcribed speech and is unlikely to beat existing fine-tunes.
- **Mixed-script output** (Hindi in Devanagari, English in Latin) remains unsolved for CPU-class models.
