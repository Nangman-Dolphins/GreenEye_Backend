import "influxdata/influxdb/schema"
schema.tagKeys(bucket: "sensor_data", predicate: (r) => r._measurement == "sensor_readings")