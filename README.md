<div align="center">

# 📚 Agilent.com Documentation Archive

### 🔬 Manuals • 🎓 Education • 🔧 Repair & Maintenance Guides • 🤖 AI-Ready Knowledge

_A community-driven collection of PDF documentation from the official Agilent website, organized so that students, technicians, researchers, and AI tools can all learn from it._

![Status](https://img.shields.io/badge/status-active-brightgreen)
![Format](https://img.shields.io/badge/format-PDF-red)
![AI Ready](https://img.shields.io/badge/AI-ready-blueviolet)
![Contributions](https://img.shields.io/badge/contributions-welcome-orange)
![Unofficial](https://img.shields.io/badge/Agilent-unofficial%20archive-lightgrey)

**🌐 Official source: [agilent.com](https://www.agilent.com/)**

</div>

> ⚠️ **Unofficial archive.** This project is **not** created, endorsed, or maintained by Agilent Technologies, Inc. All documents remain the property of their respective owner. Please read the [Legal Notice](#%EF%B8%8F-legal-notice--copyright) before using or sharing anything here.

---

## 📑 Table of Contents

1. [🎯 Mission](#-mission)
2. [💡 Why This Repository Exists](#-why-this-repository-exists)
3. [📁 Repository Structure](#-repository-structure)
4. [📄 What's Inside the `PDFs/` Folder](#-whats-inside-the-pdfs-folder)
5. [✨ Benefits of These PDFs](#-benefits-of-these-pdfs)
6. [👥 Who Is This For?](#-who-is-this-for)
7. [🔬 Product Areas Covered](#-product-areas-covered)
8. [🚀 Getting Started](#-getting-started)
9. [🤖 How to Use These Docs With AI](#-how-to-use-these-docs-with-ai)
10. [🧑‍💻 Developer Guide: Turn PDFs Into an AI Knowledge Base](#-developer-guide-turn-pdfs-into-an-ai-knowledge-base)
11. [⚠️ Safety First](#%EF%B8%8F-safety-first)
12. [❓ FAQ](#-faq)
13. [🗺️ Roadmap](#%EF%B8%8F-roadmap)
14. [🤝 Contributing](#-contributing)
15. [⚖️ Legal Notice & Copyright](#%EF%B8%8F-legal-notice--copyright)
16. [🔗 Official Links](#-official-links)

---

## 🎯 Mission

> **Make high-quality scientific and technical knowledge easy to find, easy to understand, and free to learn from, for everyone, everywhere.**

Behind every lab result, medicine, food-safety test, environmental measurement, and research breakthrough there is a piece of equipment, and behind that equipment is a manual. Too often those manuals are hard to find, scattered across many pages, or locked away from the people who need them most: students, small labs, schools, and technicians in places with limited support.

This repository brings that documentation together in one place and shows how modern **AI tools** can turn dense technical PDFs into clear, personalized help. 💙

---

## 💡 Why This Repository Exists

| Problem 😩                                           | How this repo helps ✅                                 |
| ---------------------------------------------------- | ------------------------------------------------------ |
| Manuals are scattered across hundreds of web pages   | 📂 One organized folder of PDFs                        |
| Old instruments lose online support over time        | 🗄️ Documents stay available for learning and reference |
| Technical language is hard for beginners             | 🤖 AI can explain any page in plain words              |
| Slow or expensive internet access                    | 📥 Download once, read offline anytime                 |
| Language barriers                                    | 🌍 AI can translate documents on demand                |
| Small labs and schools have limited training budgets | 🎓 Free self-study material                            |
| Hard to search across many manuals                   | 🔍 Searchable text, ready for AI retrieval             |

---

## 📁 Repository Structure

```text
agilent-com-documentation/
│
├── 📄 README.md        ← You are here
├── 📜 LICENSE          ← License for the original content of this repo (see Legal Notice)
│
└── 📂 PDFs/            ← 📚 All documentation PDFs live here
    ├── 🔬 manuals/
    ├── 🧪 application-notes/
    ├── 📊 datasheets/
    ├── 🛠️ maintenance-and-repair/
    └── ...
```

> 📝 **Note:** the sub-folders above are a suggested layout. Adjust them to match what is actually inside `PDFs/`.

### 🏷️ Suggested file naming

Clear names make documents easier for humans **and** AI to find:

```text
[product-or-series]_[document-type]_[part-number-or-edition].pdf

Examples:
  cary-630-ftir_operation-manual_edition-10.pdf
  1260-infinity-lc_maintenance-guide_G1310-90000.pdf
  microlab-software_operation-manual_rev-5.7.pdf
```

---

### ⚙️ Install the requirements and run the script

```bash
# Install required applications
sudo apt-get install bash git ghostscript coreutils util-linux findutils grep sed gawk psmisc openssh-client ca-certificates python3 python3-pip python3-venv python3-lxml python3-psutil iputils-ping xvfb nodejs -y

# Clone the repository
git clone --depth 1 https://github.com/PrajwalKoirala638/agilent-com-documentation

# Enter the folder
cd agilent-com-documentation

# Create a virtual environment
python3 -m venv .venv

# Activate the virtual environment
source .venv/bin/activate

# Install the Python packages required by the project.
pip install -r requirements.txt

# Install the required Python packages
pip install playwright invisible-playwright

# Install Playwright system dependencies
playwright install --with-deps

# Install Playwright Chromium
playwright install chromium

# Install Playwright Firefox
playwright install firefox

# Install Invisible Playwright Engine Using Invisible Playwright
invisible-playwright fetch

# Install Invisible Playwright Engine Using Python
python3 -m invisible_playwright fetch

# Run the script
python3 download_sds.py
```

---

## 📄 What's Inside the `PDFs/` Folder

Every file in the **`PDFs/`** folder is a document published on or linked from the official Agilent website. Typical document types include:

| 📦 Document type                       | 📝 What it contains                                                            | 🌟 Why it's valuable                                   |
| -------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------ |
| 📘 **Operation / User Manuals**        | Setup, daily use, software workflows, specifications                           | The definitive "how do I use this?" reference          |
| 🚀 **Quick-Start Guides**              | Short first-run instructions                                                   | Get running in minutes, great for beginners            |
| 🛠️ **Maintenance & Repair Guides**     | Routine care, part replacement, cleaning, calibration                          | Keeps instruments alive longer and saves service costs |
| 🔍 **Troubleshooting Guides**          | Error codes, symptoms, likely causes, fixes                                    | Cuts downtime and guesswork                            |
| 🧰 **Installation / Site-Prep Guides** | Space, power, gas, network, and environment requirements                       | Avoid costly setup mistakes                            |
| 🧪 **Application Notes**               | Real-world methods and example results                                         | Learn proven analytical techniques                     |
| 📊 **Datasheets & Specifications**     | Performance figures, dimensions, supported options                             | Compare and select equipment with confidence           |
| 📚 **Primers & Educational Guides**    | Fundamentals of chromatography, spectroscopy, mass spec, sample prep, and more | Free textbook-style learning                           |
| 🛡️ **Safety Documents**                | Hazard information, handling and safety notices                                | Protects people and equipment                          |
| 💻 **Software Manuals**                | Data-system, instrument-control, and analysis software guides                  | Master the software, not just the hardware             |
| ✅ **Compliance & Validation Guides**  | Qualification, calibration, and regulated-lab procedures                       | Support quality and audit readiness                    |

---

## ✨ Benefits of These PDFs

### 1. 🎓 Free, High-Quality Education

Professional-grade technical documents are some of the best learning material available. They show how real instruments work, how real methods are built, and how professionals think about quality. A student can learn the principles behind **chromatography, spectroscopy, mass spectrometry, genomics, and cell analysis** straight from the source.

### 2. 🔧 Lower Repair & Maintenance Costs

Many everyday problems (a noisy baseline, a clogged line, a worn seal, a routine calibration) have documented procedures. With the right manual, trained staff can handle **preventive maintenance and basic troubleshooting** themselves and know exactly when to call a professional.

### 3. ⏱️ Less Downtime

A lab that stops running loses time, samples, and money. Having troubleshooting guides **instantly available offline** means faster diagnosis and quicker recovery.

### 4. 🛡️ Safer Labs

Manuals contain safety warnings, hazard notices, and correct operating limits. Easy access means people actually **read and follow** them before working with high voltage, high pressure, hot parts, or hazardous chemicals.

### 5. 🌍 Access Without Barriers

- 📶 Works **offline** once downloaded
- 🌐 Can be **translated** into almost any language with AI
- 💸 **Free** to read, study, and learn from
- 🏫 Ideal for **schools, universities, and community labs**

### 6. 🤖 AI Multiplies the Value

A PDF alone is static. With AI it becomes **interactive**:

- Ask questions in plain language and get answers grounded in the document
- Get step-by-step explanations adjusted to your level
- Generate checklists, quizzes, cheat sheets, and training material
- Compare several instruments or methods side by side

### 7. 📈 Better Purchasing Decisions

Datasheets and specification guides make it easy to **compare performance, requirements, and capabilities** before buying, upgrading, or recommending equipment.

### 8. 🧠 Preserved Knowledge

Instruments outlive webpages. Keeping documentation together helps preserve **institutional and technical knowledge** for the community, including documents for older and discontinued products.

### 9. 👩‍🏫 Ready-Made Teaching Material

Teachers and trainers can turn sections of these documents into **lesson plans, lab exercises, slides, and exams**, saving hours of preparation.

### 10. 🚀 Fuel for Open-Source & Research Projects

Clean, organized documents are a valuable dataset for **AI assistants, search tools, chatbots, and research projects** that help people use scientific equipment correctly.

---

## 👥 Who Is This For?

| 👤 You are...                              | 💡 How you can use this repo                                         |
| ------------------------------------------ | -------------------------------------------------------------------- |
| 🎓 **A student**                           | Study how real instruments and methods work; prepare for lab courses |
| 👩‍🏫 **A teacher / trainer**                 | Build lessons, exercises, quizzes, and training programs             |
| 🧑‍🔬 **A lab technician**                    | Find maintenance steps, error explanations, and setup guidance fast  |
| 🔬 **A researcher**                        | Check specifications and methods before designing experiments        |
| 🏭 **A quality / compliance professional** | Reference procedures, qualification, and safety guidance             |
| 🏫 **A small lab or school**               | Get documentation without a big support budget                       |
| 💻 **A developer / AI builder**            | Build assistants, chatbots, and search tools on top of the documents |
| 🌱 **A curious learner**                   | Explore how modern science is measured, step by step                 |

---

## 🔬 Product Areas Covered

Agilent is best known for **life-science and chemical-analysis instruments**. Documentation in the `PDFs/` folder may relate to areas such as:

| 🧬 Area                           | 🔧 Example technologies                                            |
| --------------------------------- | ------------------------------------------------------------------ |
| 💧 **Liquid Chromatography (LC)** | HPLC and UHPLC systems, detectors, autosamplers, columns           |
| 🔥 **Gas Chromatography (GC)**    | GC systems, detectors, inlets, columns, consumables                |
| ⚛️ **Mass Spectrometry (MS)**     | LC/MS, GC/MS, ICP-MS, tandem and high-resolution systems           |
| 🌈 **Spectroscopy**               | UV-Vis, FTIR, atomic absorption, ICP-OES, microwave plasma         |
| 🧫 **Cell Analysis**              | Cell metabolism, flow cytometry, live-cell assays                  |
| 🧬 **Genomics & Diagnostics**     | PCR/qPCR, sequencing workflows, sample prep, target enrichment     |
| 🤖 **Automation & Sample Prep**   | Liquid handling, automated workflows, consumables                  |
| 🧰 **Consumables & Supplies**     | Columns, vials, filters, standards, parts                          |
| 💻 **Software & Data Systems**    | Chromatography data systems, instrument control, analysis software |
| ✅ **Compliance & Services**      | Qualification, calibration, validation, regulated workflows        |

> 💬 _Historical note:_ Agilent's electronic-measurement business became a separate company (Keysight Technologies) in 2014, so older electronics test-and-measurement documents may reference either name.

---

## 🚀 Getting Started

### 🌐 Option 1: Browse on GitHub

Open the **`PDFs/`** folder and click any file to preview it. Press `/` on GitHub to search by file name or model number.

### 📥 Option 2: Clone the whole repository

```bash
git clone https://github.com/PrajwalKoirala638/agilent-com-documentation.git
cd agilent-com-documentation
```

### 📦 Option 3: Download as ZIP

Click the green **Code** button, then **Download ZIP**. This is perfect for offline use.

### 🔍 Search tips (command line)

```bash
# List all PDFs
ls PDFs/

# Find files by name
find PDFs/ -iname "*maintenance*"

# Search inside PDFs (requires pdfgrep)
pdfgrep -rin "baseline noise" PDFs/
```

---

## 🤖 How to Use These Docs With AI

AI assistants are excellent at explaining dense technical documents, **but they are most reliable when they read the actual document** instead of answering from memory. Here are the best ways to combine the two.

### 🥇 Method 1: Upload a PDF to a chat assistant

1. 📥 Download the PDF you need from `PDFs/`
2. 📎 Attach it to an AI assistant such as [Claude](https://claude.ai), ChatGPT, or Gemini
3. ❓ Ask your question

### 🥈 Method 2: Paste a section

For one specific problem, copy only the relevant section (and your model, version, and error message) and paste it with your question. Focused context usually gives sharper answers.

### 🥉 Method 3: Build a searchable knowledge base

Load many PDFs into a "Project", "knowledge base", or retrieval (RAG) system so the AI can search across the entire collection. See the [Developer Guide](#-developer-guide-turn-pdfs-into-an-ai-knowledge-base) below.

### 💬 Example prompts

**🎓 Learn a concept**

```text
Using the attached manual, explain how this instrument works as if I'm a
first-year university student. Describe each main component and what it does.
```

**🔍 Troubleshoot a problem**

```text
My instrument shows [error message / symptom]. Using ONLY the attached
document, list possible causes and the recommended steps. If the document
doesn't cover it, say so. Quote the page or section you used.
```

**🛠️ Plan maintenance**

```text
Create a daily, weekly, and monthly maintenance checklist from this manual.
Include recommended intervals and every safety warning.
```

**📝 Train new staff**

```text
Turn this chapter into a 20-minute training lesson with learning objectives,
key points, and 10 quiz questions with answers.
```

**🌍 Translate**

```text
Translate the installation section into [language] in simple words.
Keep ALL safety warnings accurate and clearly marked.
```

**📋 Make a cheat sheet**

```text
Summarize the startup and shutdown procedure as a one-page quick-reference card.
```

**⚖️ Compare options**

```text
Compare these two datasheets in a table: key specifications, requirements,
and best-fit applications.
```

**🧾 Build SOP drafts**

```text
Draft a Standard Operating Procedure outline based on this manual.
Mark every step that needs verification by a qualified person.
```

### ✅ Tips for the best results

| ✔️ Do                                                     | ❌ Avoid                                           |
| --------------------------------------------------------- | -------------------------------------------------- |
| Tell the AI to **answer only from the document**          | Letting the AI guess from memory                   |
| Include **exact model, software version, and error text** | Vague questions like "it's broken"                 |
| Ask the AI to **quote the page or section**               | Trusting answers with no source                    |
| **Verify** important steps in the original PDF            | Acting on AI output without checking               |
| Compare with the **latest official version**              | Assuming an old manual matches your newer firmware |
| Ask for **step-by-step** answers                          | Skipping the safety warnings                       |

---

## 🧑‍💻 Developer Guide: Turn PDFs Into an AI Knowledge Base

### Step 1️⃣: Install tools

```bash
pip install pypdf
```

### Step 2️⃣: Extract text from every PDF

```python
from pathlib import Path
from pypdf import PdfReader

out = Path("text")
out.mkdir(exist_ok=True)

for pdf in Path("PDFs").glob("**/*.pdf"):
    reader = PdfReader(pdf)
    pages = [(p.extract_text() or "") for p in reader.pages]
    text = "\n\n".join(f"[Page {i+1}]\n{t}" for i, t in enumerate(pages))
    (out / f"{pdf.stem}.txt").write_text(text, encoding="utf-8")
    print(f"✅ {pdf.name}: {len(pages)} pages")
```

> 🖼️ **Scanned PDFs?** If a PDF is only images, run OCR first (for example with `ocrmypdf`) so the text can be searched.

### Step 3️⃣: Split into chunks

```python
def chunk(text, size=1500, overlap=200):
    step = size - overlap
    return [text[i:i + size] for i in range(0, len(text), step)]
```

### Step 4️⃣: Index and search

Store the chunks in a vector database or search index (for example Chroma, FAISS, or SQLite FTS), keeping the **file name and page number** with every chunk.

### Step 5️⃣: Ask questions with sources

Retrieve the best-matching chunks, pass them to an AI model along with the question, and instruct it to **answer only from those chunks and cite file and page**.

### 💡 Project ideas

- 🤖 A chatbot that answers questions about instruments, with page citations
- 🔎 A multilingual search engine for the documentation
- 🎓 An automatic quiz and flashcard generator for lab courses
- 🛠️ A troubleshooting assistant: _symptom → likely cause → documented fix_
- 📱 An offline mobile reference app for technicians

---

## ⚠️ Safety First

Laboratory instruments can involve **high voltage ⚡, high pressure 💨, hot surfaces 🔥, toxic or flammable chemicals ☣️, lasers 🔦, and radiation ☢️.**

- 🤖 **AI can make mistakes.** Never rely on AI output alone for safety-critical work.
- 📖 Read and follow every **WARNING** and **CAUTION** notice in the original manual.
- 🔌 Power down and follow proper lockout procedures **before** opening any instrument.
- 🧑‍🔧 Repairs that need special training, or that could affect warranty or calibration, should be done by **qualified technicians or official Agilent service**.
- 🥽 Use the correct protective equipment and follow your lab's safety rules.
- 📞 **When in doubt, stop and ask** an expert or contact [Agilent support](https://www.agilent.com/en/support).

> 🧪 Documents here may be **old editions**. Always confirm procedures against the manual that matches your exact instrument, firmware, and software version.

---

## ❓ FAQ

**❔ Is this an official Agilent project?**
No. It is an independent, community-run archive. Always use [agilent.com](https://www.agilent.com/) as the official source.

**❔ Are these documents up to date?**
Not necessarily. Manuals are revised over time. Check the edition and date inside each PDF and compare with the latest version on the official website.

**❔ Can I use these PDFs with AI tools?**
Yes, for personal learning and study, uploading a PDF to an AI assistant is a great way to understand it. If you plan a public or commercial AI product, review the [Legal Notice](#%EF%B8%8F-legal-notice--copyright) first.

**❔ Can I fix my instrument just by following these guides?**
Only for tasks the manual says users may do, and only if you are trained and following all safety instructions. Otherwise contact qualified service.

**❔ My PDF can't be searched or copied. Why?**
It may be a scanned image. Use an OCR tool to make it searchable.

**❔ Something is missing or wrong. What do I do?**
Open an [Issue](https://github.com/PrajwalKoirala638/agilent-com-documentation/issues) and tell us.

---

## 🗺️ Roadmap

- [x] 📂 Collect documentation PDFs in the `PDFs/` folder
- [x] 📖 Write a complete README
- [ ] 🗂️ Organize PDFs into clear sub-folders by product and document type
- [ ] 📇 Add an index (`INDEX.md` or CSV) listing every document with title, product, type, and edition
- [ ] 🔤 Add searchable text versions of every PDF
- [ ] 🤖 Provide ready-to-use scripts and AI prompt templates
- [ ] 🌍 Community translations of summaries and guides
- [ ] 🎓 Study guides and quizzes built from the documents
- [ ] ✅ Regular checks that documents still match the official website

---

## 🤝 Contributing

Contributions of every size are welcome! 🎉

1. 🍴 **Fork** this repository
2. 🌿 Create a branch: `git checkout -b improve-docs`
3. ✏️ Make your changes (add documents, improve organization, fix mistakes)
4. 💾 Commit: `git commit -m "Describe your change"`
5. 🚀 Push and open a **Pull Request**

### 🙌 Ways to help

- 📥 Add missing documents from the official website, with the source link
- 🗂️ Improve folder structure and file names
- 📇 Help build the document index
- 🔤 Provide OCR/text versions of scanned PDFs
- 🌍 Translate summaries and guides
- 🐛 Report outdated, broken, or incorrect files via Issues
- 💡 Share helpful AI prompts and workflows

### 📏 Guidelines

- ✅ Only add documents that the official Agilent website makes publicly available
- 🔗 Record the **original source URL** for every document you add
- 🚫 Do **not** upload confidential, paywalled, or private/internal documents
- 🤝 Be respectful and helpful to everyone

---

## ⚖️ Legal Notice & Copyright

- 📜 All documentation in the `PDFs/` folder originates from **Agilent Technologies, Inc.** and its website, [agilent.com](https://www.agilent.com/). **Copyright and all rights in those documents remain with Agilent Technologies, Inc. (or the respective rights holders).**
- 🧾 The repository's **LICENSE applies only to the original material created for this project** (such as this README, scripts, and indexes). **It does not grant any rights to Agilent's documents.** Your use of those documents is subject to Agilent's own terms.
- ™️ _Agilent_ and related names and logos are trademarks of Agilent Technologies, Inc., used here only to identify the source of the documents.
- 🚫 This repository is **not affiliated with, sponsored by, or endorsed by** Agilent.
- ⚠️ Content is provided **"as is"**, with no warranty of accuracy, completeness, or currency. Always use the official source for the latest version.
- 📚 This collection is shared for **educational and informational purposes**.
- 📩 **Rights holders:** if you represent Agilent and would like any document corrected or removed, please open an Issue or contact the maintainer, and it will be addressed promptly.

---

## 🔗 Official Links

| 🔗 Resource        | 🌐 Link                                      |
| ------------------ | -------------------------------------------- |
| 🏠 Agilent website | <https://www.agilent.com/>                   |
| 🛟 Agilent support | <https://www.agilent.com/en/support>         |
| 📬 Contact Agilent | <https://www.agilent.com/en/contact-us/page> |

---

## 👤 Maintainer

Maintained by **[@PrajwalKoirala638](https://github.com/PrajwalKoirala638)**.

---

<div align="center">

### 💙 If this project helps you, please ⭐ **star the repo** and share it!

_Knowledge grows when it is shared._ 🌍

**Learn • Repair • Teach • Build • Share**

</div>
