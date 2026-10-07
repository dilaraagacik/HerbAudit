<p align="center"><img src="assets/logo.svg" width="520" alt="HerbAudit"></p>

An AI-powered audit tool for herbarium specimen digitization. HerbAudit takes
photographed herbarium sheets, extracts the label data (collector, date,
locality, taxon name, etc.), and checks the result against reference sources
so digitization errors can be caught automatically instead of by hand.

## What it does

1. **Detects the label region** on a specimen photo using a YOLO-family
   archival component detector (the vendored LeafMachine2 model, or any
   `ultralytics`-trained checkpoint), then crops and collages it for the
   extraction step.
2. **Extracts label text** with a vision-capable LLM — Gemini, OpenAI, or a
   local model served through [Ollama](https://ollama.com).
3. **Cross-checks the result**:
   - Taxon names against [World Flora Online](http://www.worldfloraonline.org/) (WFO)
   - Records against [GBIF](https://www.gbif.org/) (unless run with `--no-reference`)
   - Gold Standard Dataset [available here](https://cloud.ilabt.imec.be/index.php/s/EZ3ybyr2a9t49Sn?dir=/): 90 manually annotated herbarium sheets from 63 different institutions, some chosen from [Dillen et al. (2019)](https://doi.org/10.3897/BDJ.7.e31817)
4. **Reports** the audit as an `.html` summary and `.xlsx` export, including
   per-call cost tracking for cloud models.
   
> **Platform note:** HerbAudit has been developed and tested on **Linux** only
> (Debian, including under WSL2). Other platforms (native Windows, macOS) are
> not tested and may not work. If you are on Windows, use WSL2.

**You can test HerbAudit on [Google Colab](https://colab.research.google.com/drive/1mKSacPXV8qViAztk5q4uMUj3vLm311mA?usp=sharing).**

## Install

```bash
git clone git@github.com:dilaraagacik/HerbAudit.git
cd HerbAudit

python3 -m venv venv_herbaudit
source venv_herbaudit/bin/activate      # Windows: venv_herbaudit\Scripts\activate

pip install -e ./herbaudit

```
Try it out and see whether it was uploaded successfully.
```bash
herbaudit --help
```
Copy the example config and set an API key (only needed for Gemini/OpenAI —
skip this for a local Ollama model):

```bash
mkdir -p ~/.herbaudit
cp config.example.toml ~/.herbaudit/config.toml
echo "GEMINI_API_KEY=your-key-here" >> ~/.herbaudit/.env
```

## Model weights (label detector)

The label/archival-region detector needs a trained `.pt` checkpoint or you can simply use opencv.

### LeafMachine2/YOLOv5 backend (default) — `archival_detector_best.pt`

**Downloads automatically the first time you run `herbaudit`** — no manual
step needed. On first use, if the checkpoint isn't cached yet, HerbAudit downloads LeafMachine2's own
official release (~1.4GB, one-time), extracts just the Archival Component
Detector's checkpoint, and caches it at `~/.herbaudit/models/archival_detector_best.pt`

If the auto-download ever fails, it
logs a warning and falls back to OpenCV rather than crashing. To recover
manually:

```bash
curl -L -o /tmp/leafmachine2_release.zip \
  https://github.com/Gene-Weaver/LeafMachine2/releases/download/v-2-1/release_v-2-1.zip
unzip -p /tmp/leafmachine2_release.zip release_v-2-1/acd/best.pt \
  > ~/.herbaudit/models/archival_detector_best.pt
```







## Usage


```bash
herbaudit --input herbaudit/test/5173738301.jpg --model gemini-3.5-flash-lite 
```

The `--model` name alone picks the provider — no separate `--provider` flag:

| Model name starts with              | Provider |
|--------------------------------------|----------|
| `gemini*`                            | Gemini   |
| `gpt*`, `o1*`, `o3*`, `o4*`, `chatgpt*`, `text-*` | OpenAI |
| anything else                        | local Ollama (needs `--ollama-host`, default `http://localhost:11434`) |

Common flags:

```bash
herbaudit --input ./scans --model qwen2.5vl:7b --ollama-host http://localhost:11434
herbaudit --input ./scans --model gemini-2.5-flash --no-reference   # skip evaluation get transcription results only
herbaudit --input ./scans --model gemini-2.5-flash --detector opencv   # skip the YOLO detector
```

Run `herbaudit --help` for the full flag reference.

## Reference data and annotations

By default each specimen is checked against its GBIF record, matched by the
image filename (without extension). `--input` can also be a CSV or Excel file
of existing transcriptions, which are evaluated as they are, without a new
transcription step.

GBIF is not always complete or correct, so you can add your own information in
one JSON file. Pass it with `--annotations` (a packaged file is used if you
omit it; `--annotations ""` turns it off). Each key is an image filename
without extension, and each entry has two optional parts for two situations:

- **The specimen is not on GBIF: give `fields`**, the correct values
  (`scientificName`, `genus`, `specificEpithet`, `recordedBy`, `eventDate`,
  `catalogNumber`, `country`, `stateProvince`, `locality`, `decimalLatitude`,
  `decimalLongitude`). The AI is scored against them instead of the specimen
  being skipped.
- **The specimen is on GBIF but a value may be wrong: give `transcription`**,
  the label text as printed. When the AI and the reference disagree on a field,
  HerbAudit looks for each value in this text, and the value found on the label
  becomes the reference. If neither is found, the reference stays as it was.

```json
{
  "GENT10099346": {
    "fields": {
      "scientificName": "Anonidium mannii (Oliv.) Engl. & Diels",
      "recordedBy": "Léonard, J.",
      "eventDate": "1946-08-29",
      "country": "Democratic Republic of the Congo",
      "locality": "Km 26, route Bikoro"
    }
  },
  "1839047232": {
    "transcription": "No 509. ex musei herbario parisiensis Nicolasia Quinqueseta O. Hoffm. ex. Thell. ... Dinter 509. Gr. Barmen, 1300 m Namibie 15 mai 1907 ..."
  }
}
```

The first specimen is not on GBIF, so its values come from `fields`. The second
is on GBIF, and its label text settles any disagreement.

You are responsible for checking that the reference is correct.

## Citation

HerbAudit's default label detector is the Archival Component Detector from
LeafMachine2. If you use HerbAudit with that detector, please also cite:

> Weaver, W. N., & Smith, S. A. (2023). From leaves to labels: Building
> modular machine learning networks for rapid herbarium specimen analysis
> with LeafMachine2. *Applications in Plant Sciences*, 11(5), e11548.
> https://doi.org/10.1002/aps3.11548
