import sqlite3
conn = sqlite3.connect('vidsort.db')

folder = '//192.168.0.110/new_ark/그거'
active_exts = sorted(['.mp4','.mkv','.avi','.mov','.wmv',
                      '.flv','.m4v','.3gp','.mts','.vob','.rmvb','.divx'])
# webm, ts 제외

params = []
where  = []

# 포맷 필터
ph = ','.join('?'*len(active_exts))
where.append(f"f.ext IN ({ph})")
params.extend(active_exts)

# 폴더
where.append("f.folder = ?")
params.append(folder)

where_sql = "WHERE " + " AND ".join(where)

count_sql = f"SELECT COUNT(*) FROM files f {where_sql}"
data_sql  = f"SELECT f.* FROM files f {where_sql} ORDER BY f.name COLLATE NOCASE ASC LIMIT 500 OFFSET 0"

total = conn.execute(count_sql, params).fetchone()[0]
rows  = conn.execute(data_sql,  params + []).fetchall()
print(f"count: {total}")
print(f"rows:  {len(rows)}")

# ext 값 확인
exts = conn.execute(f"SELECT DISTINCT ext FROM files f {where_sql}", params).fetchall()
print(f"ext 종류: {[r[0] for r in exts]}")

conn.close()