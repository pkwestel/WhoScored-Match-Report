import pandas as pd
import requests
from fake_useragent import UserAgent


def load_fbref_player_data(page_url, table_index=0):
    """
    Load player data from a given FBref page URL.

    Parameters:
    page_url (str): The URL of the FBref page containing player data.
    table_index (int): Index of the table to return (default: 0 for first table)

    Returns:
    pd.DataFrame: A DataFrame containing the player data, or None if error occurs.
    """
    try:
        # Use fake user agent to mimic a real browser request
        ua = UserAgent()
        headers = {"User-Agent": ua.random}

        # Make a GET request to the page URL
        response = requests.get(page_url, headers=headers)
        response.raise_for_status()

        # Parse all tables from the page using pandas
        tables = pd.read_html(response.text)

        # Check if any tables were found
        if not tables:
            print("No tables found on the webpage")
            return None

        # Check if the requested table index exists
        if table_index >= len(tables):
            print(
                f"Table index {table_index} not found. Only {len(tables)} tables available"
            )
            return None

        # Select the desired table based on the provided index
        player_data = tables[table_index]
        print(
            f"Successfully loaded table {table_index + 1} of {len(tables)} tables found"
        )

        return player_data

    except ValueError as e:
        print(f"Error parsing HTML tables from {page_url}: {str(e)}")
        return None
    except Exception as e:
        print(f"Error loading data from {page_url}: {str(e)}")
        return None


if __name__ == "__main__":
    # Example usage
    url = "https://fbref.com/en/comps/Big5/stats/players/Big-5-European-Leagues-Stats"
    # url = "https://fbref.com/en/comps/22/stats/Major-League-Soccer-Stats" # Will not work due to JS rendering
    df = load_fbref_player_data(url, table_index=0)

    if df is not None:
        print(df.head())
    else:
        print("Failed to load player data")
