from(bucket: "sensor_data")
  |> range(start: -1d)
  |> filter(fn: (r) => r.device_id == "6c18")
  |> limit(n: 5)