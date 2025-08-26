import "influxdata/influxdb/schema"
schema.fieldKeys(bucket: "sensor_data", predicate: (r) => r._measurement == "sensor_readings")