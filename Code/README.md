# Review Cleaning Pipeline: Multi-ZIP Case

This pipeline cleans Google Maps reviews collected through the Google Places API for the multi-ZIP
case (Manhattan 10002, 10012, 10013, plus Bronx 10458) and screens them for signs of foodborne
illness. It handles language and translated text, removes duplicate reviews, and scores each review
against six evidence buckets. The original text is always kept, and every score records the words
that produced it.

```bash
pip install pandas numpy regex lingua-language-detector opencc-python-reimplemented rapidfuzz wordfreq
python clean_reviews_multizip.py
```

## Data

The input has 9,615 reviews with restaurant ID (`CAMIS`), name, Google `place_id`, review text,
rating, timestamp, reviewer name, and ZIP code. Two limits of the Places API matter here:

- It returns at most 5 reviews per place, so the data is a sample of recent reviews.
- It sometimes returns a non-English review already translated into English, without the original.

## Steps

**1. Check and standardize.** The script stops if any row is misaligned (from an earlier export
problem) instead of guessing a repair. Ratings become integers, and timestamps are stored in UTC and
New York time.

**2. Normalize text.** The raw review is never changed. A clean copy fixes Unicode, quotes, and
spacing. A separate matching copy removes accents (`intoxicación` → `intoxicacion`), converts
traditional Chinese to simplified, and corrects common misspellings of symptom words
(`dirrea` → `diarrhea`). Real English and Spanish words are never "corrected."

**3. Detect language (Lingua).** Lingua labels each review's language with a confidence score. A
label counts as reliable only with confidence ≥ 0.5 and at least 4 words, since short reviews like
"Good" were often mislabeled as Somali or Zulu. The script also records which writing systems a
review uses, to catch mixed text such as English with Chinese dish names.

- *Translated reviews:* reviews Google already translated read as English and are scored with the
  English rules. They can't be told apart from reviews written in English.
- *Uncovered languages:* the rules fully cover English and Spanish. A review reliably in another
  language that matches nothing is sent to a person to translate and label, because "no match" does
  not mean "no illness" there.

**4. Remove duplicates.** A review is identified by `place_id` + reviewer + timestamp. When one
Google listing is matched to several restaurants (e.g., a food hall with 9 vendors), its reviews
appear once per restaurant. One copy is kept, all matching restaurant IDs are listed, and the review
is flagged as unattributable to a single vendor. Exact repeats are dropped, and the same long text
posted by one reviewer at different places is flagged. This removed 402 copies, leaving 9,213 reviews.

**5. Protect privacy.** Reviewer names are replaced with a hashed ID.

## Six evidence buckets

Each review is checked against six groups of terms. Every language's terms run on every review,
whatever Lingua says, which catches mixed-language text and mislabeled short reviews.

| Bucket | Looks for | Examples |
|---|---|---|
| Symptom | Illness symptoms | food poisoning, vomiting, diarrhea, intoxicación, 拉肚子 |
| Link | Timing tied to the meal | "after eating", "hours later", "the next day" |
| Multiple | More than one person sick | "my wife and I", "we all", "toda la familia" |
| Medical | Medical care | hospital, ER, urgent care, doctor |
| Food safety | Food handling problems | undercooked, raw chicken, spoiled, expired |
| Hygiene | Conditions at the restaurant | roaches, rats, hair in food, dirty |

English and Spanish have terms in all six buckets; Chinese, Japanese, Hebrew, and Russian have
symptom terms. Extra rules cut false matches:

- **Negation:** "didn't get sick" or "hoping I don't get sick" does not count.
- **Ambiguous words:** bare "sick" or "stomach" counts only in a sentence about the reviewer, so
  "sick vibe" and "can't stomach the prices" are ignored.
- **Emoji:** 🤢 🤮 🤒 are weak evidence and can never decide a review on their own.

**Scoring.** A symptom scores highest, and more when supported by another bucket (link, multiple,
medical, or food safety) and a 1–2 star rating. Food-safety and hygiene complaints without a symptom
score lower. Each review falls into a band:

| Band | Meaning | Count |
|---|---|---:|
| 1 | Strong indication (score ≥ 0.8) | 37 |
| Grey | Uncertain (0.2–0.8); needs a person | 115 |
| 0 | No indication (< 0.2) | 6,462 |
| Foreign | Uncovered language, no match | 68 |
| No text | Rating only | 2,531 |

The score is a transparent screening rule, not a probability.

## Human labeling

A labeling sheet collects every band-1, grey, and foreign review, plus a random 10% of band 0 to
check for missed cases. Reviewers fill in a label (0, 1, or in between) and notes.
