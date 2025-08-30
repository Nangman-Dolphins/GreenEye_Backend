from werkzeug.security import generate_password_hash
import sqlite3
email="softyware21@gmail.com"
new_pw="kitel1976!"
h=generate_password_hash(new_pw)
con=sqlite3.connect("/app/data/greeneye_users.db")
cur=con.cursor()
cur.execute("UPDATE users SET password_hash=? WHERE email=?", (h,email))
con.commit()
print(f"[OK] reset password for {email} -> {new_pw}")
