from backend_app.database import get_db_connection
c = get_db_connection()
c.execute("UPDATE devices SET room='Living Room' WHERE device_id='eef1'")
c.execute("UPDATE devices SET room='Bedroom'     WHERE device_id='eef2'")
c.execute("UPDATE devices SET room='Office'      WHERE device_id='eef9'")
c.execute("UPDATE devices SET plant_type='Rhododendron', room='Balcony' WHERE device_id='6c18'")
c.commit()
print('OK - device rows updated')