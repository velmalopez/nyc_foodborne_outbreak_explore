# Data Pulling and Cleaning

This document describes how the Google review dataset (`Data/dataset.csv`) was built: which restaurants were selected, how their Google reviews were pulled, and how the reviews were cleaned and labeled for possible foodborne illness.

## Pipeline at a glance

```
NYC DOHMH inspection exports (by ZIP code)
        │  deduplicate on CAMIS
        ▼
restaurants_combined.csv ── one row per restaurant
        │  Google Places "Find Place" → place_id
        │  Google Places "Place Details" → reviews
        ▼
reviews.csv / reviews_translated.csv / reviews_10003.csv ── raw reviews
        │  drop empty reviews, normalize text, extract emoji
        │  exact + fuzzy symptom keyword matching
        │  manual review of flagged reviews
        ▼
dataset.csv ── cleaned, labeled reviews for modeling
```



## 1. Selecting restaurants

Restaurants come from the **DOHMH New York City Restaurant Inspection Results** dataset on NYC Open Data, exported for a few ZIP codes at a time. The inspection data has one row per inspection or violation, so the same restaurant shows up many times. Each export was deduplicated on `CAMIS` (the DOHMH's unique restaurant ID). Only the restaurant-level columns were kept:

`CAMIS, DBA, BORO, BUILDING, STREET, ZIPCODE, PHONE, Latitude, Longitude, Location`

New exports were appended to `Data/restaurants_combined.csv` and deduplicated on `CAMIS` again.


## 2. Matching restaurants to Google Places

Each restaurant was matched to a Google Maps listing with the Places API **Find Place** endpoint (`googlemaps` Python client). The text query combines the name and address:

```
"{DBA} {BUILDING} {STREET} {BORO} NY {ZIPCODE}"
```

The first candidate's `place_id` is used. A few restaurants returned no candidate and were dropped (3 in the first batch).

## 3. Pulling reviews

For each `place_id`, reviews were requested from the **Place Details** endpoint with these settings:

- `fields=["name", "reviews"]`
- `reviews_sort="newest"`, so the most recent reviews come first
- `language="en"` and `reviews_no_translations=False`, so Google returns an English translation of non-English reviews (added in the second round of pulling, see below)

Each review becomes one row:

| Column | Source |
|---|---|
| `CAMIS`, `DBA`, `place_id`, `ZIPCODE` | from the restaurant list |
| `review_text` | `review.text` |
| `rating` | `review.rating` (1–5) |
| `review_time` | `review.time` (Unix seconds), converted to `America/New_York` time |
| `Author_name` | `review.author_name` |
| `language`, `original_language`, `translated` | translation metadata from Google (translated pulls only) |

**Limit:** Place Details returns at most **5 reviews per place**, so the dataset is a sample of each restaurant's newest reviews, not its full history. That works out to about 3–4 reviews per restaurant after empty reviews are dropped.

**Resuming:** pulls were run in chunks because of API quota. `RESUME_AFTER_CAMIS` in `google_review.py` sets the last restaurant already processed, and new results are appended to `Data/reviews.csv`.





## 5. Language handling

Some reviews are in Spanish, Chinese, French and other languages. We first tried detecting the language ourselves (`Code/cleaning.ipynb`):

- **fastText** `lid.176.ftz` language-ID model
- **Lingua** detector
- two ensembles of them: a weighted average of their probabilities, and a reciprocal-rank vote

Both detectors were unreliable on short, informal reviews. For example, fastText labeled *"Best Malaysian food when I'm homesick 🌟"* as German, and *"Very good ice cream :)"* came out as Spanish. We dropped that approach and instead asked Google to return English translations (section 3). Google's metadata is kept in three columns: `original_language`, `language` and `translated`.

In the final dataset, 645 of the 10,078 reviews (about 6%) are translations. Most of these were originally Spanish, Chinese, French, German, Italian or Hebrew.

## 6. Text cleaning


1. **Drop empty reviews.** Rating-only reviews (no `review_text`) are removed. This removed about a quarter of rows in the first batch (9,615 → 6,956).
2. **Extract emoji** from the raw text into an `emoji` column with the `emoji` package, so they are available as features even though the next step removes them.
3. **Normalize text**:
   - lowercase
   - replace every character outside `a–z` and whitespace with a space (this removes punctuation, digits and emoji)
   - collapse repeated whitespace and trim
4. **Parse `review_time`** to datetime.

## 7. Labeling with symptom keywords

Each review gets a `foodborne_label` that a keyword matcher assigns first and a manual review then corrects.

### Keyword matcher

The matcher reads each 1-, 2- and 3-word n-gram of `review_clean`.

**Symptom terms (exact match):**
vomit, vomiting, vomited, diarrhea, nausea, nauseous, sick, poisoning, poison, stomach, fever, cramps, food poisoning, stomach ache, stomach cramps, upset stomach, stomachache, stomach bug, ill, illness, ecoli, e coli, hospital, hospitalized, hospitalization

**Fuzzy matching for typos.** Only terms that are long and often misspelled are fuzzy-matched, using `thefuzz` `fuzz.ratio` ≥ 85:
diarrhea, vomit, vomiting, vomited, nausea, nauseous, stomachache, poisoning, ecoli, hospitalized, hospitalization.
Short terms such as "sick" and "ill" are exact-match only, because fuzzy matching them produces many false hits.

**"Throw up".** A word is matched when it is within ratio ≥ 90 of *throw / throwing / threw* **and** the next word is "up".

**Excluded words.** *ever, never, every, stick, slick* are never matched. They come close to symptom terms but are not symptoms.

Each match is stored in `symptom_matches` as `(text found, term it matched, score)`. Fuzzy matches are then written back into the text (longest phrase first, on word boundaries) to make `review_cleaned`. For example, the typo "dirrea" becomes "diarrhea", so later models see the normalized term.

A review gets `foodborne_label = 1` if it has at least one match, and `0` otherwise.

### Manual review

Keyword matches catch many reviews that are not about illness, such as "sick" used as slang or "stomach" in "I couldn't stomach the wait". We read every flagged review by hand:

- reviews that do describe illness after eating there stay at `1`
- false positives are changed to `0`

The overrides are hard-coded by row index in `cleaned_translated.ipynb` (`keep_1` and `change_0`), once per batch of pulls.

We also compared the matcher against `Data/Flagged(Sheet1).csv`, a separately flagged set, by joining on `CAMIS` and review date in `cleaning.ipynb`. This showed which reviews each method caught that the other missed.

## 8. Final dataset: `Data/dataset.csv`

The cleaned reviews from each batch are appended to `dataset.csv`. A `stratify` column (`"{ZIPCODE}_{foodborne_label}"`) is added so that train/test splits keep both the ZIP code mix and the rare positive class balanced.

| | |
|---|---|
| Reviews | 10,078 |
| Restaurants | 2,815 |
| ZIP codes | 10002 (2,257), 10013 (2,103), 10012 (1,802), 10003 (1,740), 11354 (1,429), 10458 (747) |
| `foodborne_label = 1` | 44 (0.44%) |
| Translated reviews | 645 |

**Columns:**
`CAMIS, DBA, place_id, review_text, rating, review_time, Author_name, language, original_language, translated, review_clean, emoji, symptom_matches, review_cleaned, foodborne_label, ZIPCODE, stratify`

- `review_text`: the raw (or Google-translated) review
- `review_clean`: normalized text, before typo correction
- `review_cleaned`: normalized text with fuzzy symptom matches corrected; **this is the model input**
- `foodborne_label`: the target

## Known limitations

- **5 reviews per restaurant at most.** The Places API cap means older illness reports are not captured.
- **First-candidate place matching.** The `place_id` match is not checked, so a few restaurants may be linked to the wrong Google listing, for example when several businesses share one address.
- **Very imbalanced labels.** Only 0.44% of reviews are positive, so the models use SMOTE or class weighting and are scored on recall, precision and average precision, not accuracy.
- **Labels depend on keywords.** A review that describes illness without any of the listed terms (for example "felt awful all night") is never flagged, so it is never manually reviewed either.
- **Overrides by row index.** `keep_1` and `change_0` refer to row positions, so they are only valid for the exact batch they were written for.

