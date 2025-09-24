import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
import json
from fake_useragent import UserAgent


def load_understat_shots_data(player_url):
    """
    Load player shots data from Understat using requests and BeautifulSoup.
    Finds script tag containing 'var shotsData' and parses the JSON data.

    Parameters:
    player_url (str): The URL of the Understat player page containing shots data.

    Returns:
    pd.DataFrame: A DataFrame containing the player shots data, or None if error occurs.
    """
    try:
        # Initialize UserAgent object
        ua = UserAgent()

        # Set up headers with random user agent
        headers = {"User-Agent": ua.random}

        # Send GET request to the URL with headers
        response = requests.get(player_url, headers=headers)
        response.raise_for_status()  # Raise an exception for bad status codes

        # Parse the HTML content
        soup = BeautifulSoup(response.content, "html.parser")

        # Find all script tags
        script_tags = soup.find_all("script")

        shots_data = None

        # Look for script tag containing 'var shotsData'
        for script in script_tags:
            if script.string and "var shotsData" in script.string:
                script_content = script.string

                # Extract the JSON data using regex
                # Pattern to match: var shotsData = JSON.parse('...');
                pattern = r"var shotsData\s*=\s*JSON\.parse\('(.+?)'\)"
                match = re.search(pattern, script_content)

                if match:
                    # Get the encoded JSON string
                    encoded_json = match.group(1)

                    # Decode the escaped characters
                    try:
                        # First decode the unicode escapes
                        decoded_json = encoded_json.encode().decode("unicode_escape")

                        # Parse the JSON data
                        shots_data = json.loads(decoded_json)
                        break

                    except json.JSONDecodeError as e:
                        print(f"Error parsing JSON data: {str(e)}")
                        continue

        if shots_data is None:
            print("No shotsData found in the page")
            return None

        # Convert the JSON data to DataFrame
        df = pd.DataFrame(shots_data)

        # Convert numeric columns to appropriate data types
        numeric_columns = ["X", "Y", "xG", "minute", "h_goals", "a_goals"]
        for col in numeric_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Convert date column to datetime
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")

        return df

    except requests.RequestException as e:
        print(f"Error fetching data from {player_url}: {str(e)}")
        return None
    except Exception as e:
        print(f"Error processing data: {str(e)}")
        return None


def display_shots_data_info(df):
    """
    Display information about the scraped shots DataFrame.

    Parameters:
    df (pd.DataFrame): The shots DataFrame to display information for
    """
    if df is None:
        print("No DataFrame to display")
        return

    print(f"\nShots Data Shape: {df.shape}")
    print(f"Columns: {list(df.columns)}")

    # Show some basic statistics
    if "result" in df.columns:
        print(f"\nShot Results:")
        print(df["result"].value_counts())

    if "xG" in df.columns:
        print(f"\nxG Statistics:")
        print(f"Total xG: {df['xG'].sum():.2f}")
        print(f"Average xG per shot: {df['xG'].mean():.3f}")

    print("\nFirst 5 rows:")
    print(df.head())


def main():
    """
    Main function to demonstrate the shots data scraping functionality.
    """
    # Example Understat player URL
    player_url = "https://understat.com/player/8260"

    # Scrape shots data
    shots_df = load_understat_shots_data(player_url)

    # Display results
    display_shots_data_info(shots_df)

    return shots_df


# Execute the main function
if __name__ == "__main__":
    scraped_shots_data = main()
