"""Combine restaurant data and collect Google Maps reviews."""

from pathlib import Path
import googlemaps
import pandas as pd


API_KEY = 'XXXXXXXXX'
DATA_DIR = Path(__file__).resolve().parent.parent / "Data"

# Path to the combined restaurant data CSV file
RESTAURANTS_PATH = DATA_DIR / "restaurants_combined.csv"

# new data to be merged with existing restaurants_combined.csv
INSPECTIONS_PATH = (
    DATA_DIR / "DOHMH_New_York_City_Restaurant_Inspection_Results_20261015.csv"
)

# Path to the reviews CSV file
REVIEWS_PATH = DATA_DIR / "reviews.csv"

# The CAMIS of the last restaurant for which reviews were collected.
RESUME_AFTER_CAMIS = "50184278"



def load_restaurants():
    """Merge inspection data, keeping the first record for each CAMIS."""
    restaurants = pd.read_csv(RESTAURANTS_PATH)
    inspections = pd.read_csv(INSPECTIONS_PATH)
    new_restaurants = inspections.drop_duplicates(subset=["CAMIS"])
    new_restaurants = new_restaurants[restaurants.columns]
    return pd.concat(
        [restaurants, new_restaurants], ignore_index=True
    ).drop_duplicates(subset=["CAMIS"])


def get_place_id(row, client):
    """Find the first place matching a restaurant's name and address."""
    query = (
        f"{row['DBA']} "
        f"{row['BUILDING']} {row['STREET']} "
        f"{row['BORO']} NY {row['ZIPCODE']}"
    )
    result = client.find_place(
        input=query,
        input_type="textquery",
        fields=["place_id", "name", "formatted_address"],
    )
    candidates = result.get("candidates", [])
    return candidates[0]["place_id"] if candidates else None


def get_reviews(place_id, client):
    """Fetch the reviews Google Maps provides, newest first."""
    details = client.place(
        place_id=place_id,
        fields=["name", "reviews"],
        reviews_sort="newest",
    )
    return details.get("result", {}).get("reviews", [])


def collect_reviews(restaurants, client):
    """Collect reviews for restaurants after the configured resume point."""
    valid_restaurants = restaurants.dropna(subset=["place_id"]).copy()
    position = (
        valid_restaurants["CAMIS"]
        .astype(str)
        .eq(RESUME_AFTER_CAMIS)
        .to_numpy()
        .nonzero()[0][0]
    )
    remaining = valid_restaurants.iloc[position + 1:]
    all_reviews = []

    for _, row in remaining.iterrows():
        for review in get_reviews(row["place_id"], client):
            all_reviews.append(
                {
                    "CAMIS": row["CAMIS"],
                    "DBA": row["DBA"],
                    "place_id": row["place_id"],
                    "review_text": review.get("text"),
                    "rating": review.get("rating"),
                    "review_time": review.get("time"),
                    "Author_name": review.get("author_name"),
                    "zipcode": row["ZIPCODE"],
                }
            )

    return pd.DataFrame(all_reviews)


def save_reviews(reviews):
    """Convert timestamps to New York time and append to the reviews CSV."""
    if reviews.empty:
        return
    reviews["review_time"] = pd.to_datetime(
        reviews["review_time"], unit="s", utc=True
    ).dt.tz_convert("America/New_York")
    reviews.to_csv(REVIEWS_PATH, mode="a", header=False, index=False)


def main():
    """Update restaurant records, find place IDs, and append reviews."""
    client = googlemaps.Client(key=API_KEY)
    restaurants = load_restaurants()
    restaurants.to_csv(RESTAURANTS_PATH, index=False)
    restaurants["place_id"] = restaurants.apply(
        get_place_id, axis=1, client=client
    )
    reviews = collect_reviews(restaurants, client)
    save_reviews(reviews)


if __name__ == "__main__":
    main()
