import requests
import json


class SofaScoreAPI:
    def __init__(self):
        self.base_url = "https://www.sofascore.com/api/v1/event"

        self.endpoints = [
            "",
            "/incidents",
            "/highlights",
            "/managers",
            "/lineups",
            "/statistics",
            "/average-positions",
            "/comments",
            "/graph",
        ]

    def _make_request(self, match_id, endpoint=""):
        url = f"{self.base_url}/{match_id}{endpoint}"

        try:
            response = requests.get(url, timeout=30)
            return response
        except requests.RequestException as e:
            print(f"Request failed: {str(e)}")
            return None

    def get_match_details(self, match_id):
        response = self._make_request(match_id)
        return response.json() if response and response.status_code == 200 else None

    def get_incidents(self, match_id):
        response = self._make_request(match_id, "/incidents")
        return response.json() if response and response.status_code == 200 else None

    def get_highlights(self, match_id):
        response = self._make_request(match_id, "/highlights")
        return response.json() if response and response.status_code == 200 else None

    def get_managers(self, match_id):
        response = self._make_request(match_id, "/managers")
        return response.json() if response and response.status_code == 200 else None

    def get_lineups(self, match_id):
        response = self._make_request(match_id, "/lineups")
        return response.json() if response and response.status_code == 200 else None

    def get_statistics(self, match_id):
        response = self._make_request(match_id, "/statistics")
        return response.json() if response and response.status_code == 200 else None

    def get_average_positions(self, match_id):
        response = self._make_request(match_id, "/average-positions")
        return response.json() if response and response.status_code == 200 else None

    def get_comments(self, match_id):
        response = self._make_request(match_id, "/comments")
        return response.json() if response and response.status_code == 200 else None

    def get_graph(self, match_id):
        response = self._make_request(match_id, "/graph")
        return response.json() if response and response.status_code == 200 else None

    def get_all_data(self, match_id):
        print(f"Fetching all data for match ID: {match_id}")

        all_data = {}
        endpoint_methods = {
            "match_details": self.get_match_details,
            "incidents": self.get_incidents,
            "highlights": self.get_highlights,
            "managers": self.get_managers,
            "lineups": self.get_lineups,
            "statistics": self.get_statistics,
            "average_positions": self.get_average_positions,
            "comments": self.get_comments,
            "graph": self.get_graph,
        }

        for name, method in endpoint_methods.items():
            print(f"Fetching {name}...")
            try:
                data = method(match_id)
                all_data[name] = data
                status = "Success" if data else "Failed"
                print(f"{name}: {status}")
            except Exception as e:
                print(f"{name}: Error - {str(e)}")
                all_data[name] = None

        return all_data

    def save_data(self, data, filename="sofascore_data.json"):
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"Data saved to '{filename}'")
        except Exception as e:
            print(f"Error saving data: {str(e)}")


def main():
    api = SofaScoreAPI()
    match_id = "14566764"

    print("Getting Average Positions")
    avg_positions = api.get_average_positions(match_id)
    if avg_positions:
        print("Average positions data retrieved")

    print("\nGetting Lineups")
    lineups = api.get_lineups(match_id)
    if lineups:
        print("Lineups data retrieved")

    print("\nGetting ALL data for the match")
    all_match_data = api.get_all_data(match_id)

    api.save_data(all_match_data, f"match_{match_id}_full_data.json")


if __name__ == "__main__":
    main()
