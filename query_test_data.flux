from(bucket: "sensor_data")
  |> range(start: -90d)
  |> limit(n: 10)
