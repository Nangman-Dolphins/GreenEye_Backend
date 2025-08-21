import pandas as pd

PLANT_STANDARD_PATH = "data/plant_standards_cleaned.xlsx"

def load_plant_standards():
    df = pd.read_excel(PLANT_STANDARD_PATH)

    # 문자열 범위를 (min, max) 튜플로 변환
    def parse_range(x):
        try:
            if isinstance(x, str) and "~" in x:
                a, b = x.split("~")
                return float(a.strip()), float(b.strip())
            return x
        except:
            return None

    for col in df.columns[1:]:  # 첫 열은 식물명
        df[col] = df[col].map(parse_range)

    df.set_index(df.columns[0], inplace=True)  # 식물명을 인덱스로 설정

    return df
