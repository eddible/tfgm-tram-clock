import requests


class TFGM:
    def __init__(self, api_key, line):
        self.base_url = "https://api.tfgm.com/odata"
        self.api_key = api_key
        self.headers = {'Ocp-Apim-Subscription-Key': self.api_key}
        self.line = line

    def authenticated_get(self, endpoint):
        get_request = requests.get(
            self.base_url + f"{endpoint}", headers=self.headers
        )
        return get_request

    def get_statuses(self, obj):
        requested_values = {}
        wanted_keys = ["Carriages", "Wait", "Status", "Dest"]
        for i in range(0, 4):
            requested_values[i] = {}
            for w in wanted_keys:
                index_key = w + str(i)
                if index_key in obj:
                    if obj[index_key]:
                        requested_values[i][w] = obj[index_key]

        return requested_values

    def get_tram_status(self):
        statuses = None
        next_tram_url = f"/Metrolinks({self.line})"
        next_tram = self.authenticated_get(next_tram_url)
        if next_tram:
            next_tram_json = next_tram.json()
            statuses = self.get_statuses(next_tram_json)
            print(statuses)

        return statuses
