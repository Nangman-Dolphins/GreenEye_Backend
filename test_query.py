from services import query_influxdb_data

query = '''
from(bucket: "sensor_data")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "sensor_readings")
  |> filter(fn: (r) => r.device_id == "eef1")
  |> keep(columns: ["_time", "_field", "_value", "device_id"])
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 10)
'''

result = query_influxdb_data(query)
for row in result:
    print(row)
