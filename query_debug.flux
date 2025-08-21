from(bucket: "sensor_data")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "sensor_readings")
  |> limit(n: 5)
