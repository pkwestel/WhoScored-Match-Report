import pandas as pd
import json


def load_sofascore_json(json_data):
    """
    Convert SofaScore JSON data to separate DataFrames for home, away, and substitutions.

    Parameters:
    json_data (dict, list, or str): JSON data as dictionary, list, or JSON string

    Returns:
    tuple: (home_df, away_df, substitutions_df) or (None, None, None) if error occurs.
    """

    try:
        # If input is a string, parse it as JSON
        if isinstance(json_data, str):
            data = json.loads(json_data)
        else:
            data = json_data

        # Extract home team data
        home_df = None
        if "home" in data and data["home"]:
            home_df = pd.json_normalize(data["home"])

        # Extract away team data
        away_df = None
        if "away" in data and data["away"]:
            away_df = pd.json_normalize(data["away"])

        # Extract substitutions data
        substitutions_df = None
        if "substitutions" in data and data["substitutions"]:
            substitutions_df = pd.json_normalize(data["substitutions"])

        return home_df, away_df, substitutions_df

    except json.JSONDecodeError as e:
        print(f"JSON parsing error: {str(e)}")
        return None, None, None
    except Exception as e:
        print(f"Error converting JSON to DataFrame: {str(e)}")
        return None, None, None


def main():
    """
    Load and convert sofascore_avg_positions.json to separate DataFrames
    """

    try:
        # Load the JSON file
        with open("sofascore_avg_positions.json", "r", encoding="utf-8") as f:
            sofascore_data = json.load(f)

        print("Loading sofascore_avg_positions.json")
        home_df, away_df, substitutions_df = load_sofascore_json(sofascore_data)

        # Display home team data
        if home_df is not None:
            print(f"\nHome Team DataFrame Shape: {home_df.shape}")
            print(f"Home Columns: {list(home_df.columns)}")
            print("\nHome Team - First few rows:")
            print(home_df.head())
        else:
            print("No home team data found")

        # Display away team data
        if away_df is not None:
            print(f"\nAway Team DataFrame Shape: {away_df.shape}")
            print(f"Away Columns: {list(away_df.columns)}")
            print("\nAway Team - First few rows:")
            print(away_df.head())
        else:
            print("No away team data found")

        # Display substitutions data
        if substitutions_df is not None:
            print(f"\nSubstitutions DataFrame Shape: {substitutions_df.shape}")
            print(f"Substitutions Columns: {list(substitutions_df.columns)}")
            print("\nSubstitutions - First few rows:")
            print(substitutions_df.head())
        else:
            print("No substitutions data found")

    except FileNotFoundError:
        print("Error: sofascore_avg_positions.json file not found")
    except Exception as e:
        print(f"Error loading file: {str(e)}")


if __name__ == "__main__":
    main()
