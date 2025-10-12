import pandas as pd
import json
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
from utils.driver import get_driver


def load_whoscored_events_data(match_centre_url):
    """
    Load match centre data from WhoScored using Selenium.
    Waits for script tag to load and extracts matchCentreData JSON.

    Parameters:
    match_centre_url (str): The URL of the WhoScored match centre page.

    Returns:
    pd.DataFrame: A DataFrame containing the matchCentreData, or None if error occurs.
    """

    try:
        with get_driver() as driver:
            driver.get(match_centre_url)

            # Wait for page to load completely
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # Get page source and parse with BeautifulSoup
            page_source = driver.page_source
            soup = BeautifulSoup(page_source, "html.parser")

            # Locate the script containing matchCentreData JSON
            script_tag = soup.select_one('script:-soup-contains("matchCentreData")')

            if not script_tag:
                print("No script tag with matchCentreData found")
                return None

            # Extract JSON safely
            try:
                _, _, json_text = script_tag.text.partition("matchCentreData: ")
                match_json = json.loads(json_text.split(",\n")[0])
                print("Successfully parsed matchCentreData")

                # Extract player ID-name dictionary
                player_id_name_dict = match_json.get("playerIdNameDictionary", {})

                if not player_id_name_dict:
                    print("Warning: No player ID-name dictionary found")
                else:
                    print(f"Found {len(player_id_name_dict)} players in dictionary")

                # Extract only events if needed
                events_dict = match_json.get("events", {})

                if not events_dict:
                    print("No events data found in matchCentreData")
                    return None

                # Convert to DataFrame
                df = pd.json_normalize(events_dict)

                # Map player IDs to names if playerId column exists
                if "playerId" in df.columns:
                    # Convert playerId: float -> int -> str, handling NaN values
                    df["playerName"] = df["playerId"].apply(
                        lambda x: (
                            player_id_name_dict.get(str(int(x)))
                            if pd.notna(x)
                            else None
                        )
                    )
                    print(
                        f"Added playerName column - {df['playerName'].notna().sum()} names mapped"
                    )
                else:
                    print("Warning: No 'playerId' column found in events data")

                # Also map relatedPlayerId if it exists
                if "relatedPlayerId" in df.columns:
                    df["relatedPlayerName"] = df["relatedPlayerId"].apply(
                        lambda x: (
                            player_id_name_dict.get(str(int(x)))
                            if pd.notna(x)
                            else None
                        )
                    )
                    print(
                        f"Added relatedPlayerName column - {df['relatedPlayerName'].notna().sum()} names mapped"
                    )

                return df

            except Exception as e:
                print(f"Error parsing match data: {e}")
                return None

    except Exception as e:
        print(f"Error loading WhoScored match centre data: {str(e)}")
        return None


def main():
    """
    Main function to demonstrate the WhoScored match centre data scraping.
    """
    # Example WhoScored match centre URL
    match_url = "https://1xbet.whoscored.com/matches/1946104/live/england-league-cup-2025-2026-tottenham-doncaster"

    # Scrape match centre data
    match_df = load_whoscored_events_data(match_url)

    if match_df is not None:
        print(f"\nDataFrame Shape: {match_df.shape}")
        print(f"Columns: {list(match_df.columns)}")
        print("\nFirst few rows of the data:")
        print(match_df.head())
    else:
        print("Failed to load match centre data")

    return match_df


# Execute the main function
if __name__ == "__main__":
    scraped_match_data = main()
