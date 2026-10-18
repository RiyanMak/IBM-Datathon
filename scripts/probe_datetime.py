"""Which timestamp-parsing forms survive in this pandas build? (pandas on s390x segfaults
in some of them.) Each form runs in its own process so a crash is reported, not fatal.

    python -m scripts.probe_datetime
"""
import subprocess
import sys

FORMS = {
    "A list ISO T+seconds, format=ISO8601":
        'pd.to_datetime(["2022-09-01T00:20:00"] * 3, format="ISO8601")',
    "B list 'YYYY-MM-DD HH:MM', format=ISO8601":
        'pd.to_datetime(["2022-09-01 00:20"] * 3, format="ISO8601")',
    "C list raw 'YYYY/MM/DD HH:MM', explicit format":
        'pd.to_datetime(["2022/09/01 00:20"] * 3, format="%Y/%m/%d %H:%M")',
    "D list ISO, no format (guessed)":
        'pd.to_datetime(["2022-09-01T00:20:00"] * 3)',
    "E Series of 200 ISO, format=ISO8601 (cache path)":
        'pd.to_datetime(pd.Series(["2022-09-01T00:%02d:00" % (i % 60) for i in range(200)]), format="ISO8601")',
    "F numpy datetime64[s] from ISO text, then pd.to_datetime":
        'pd.to_datetime(np.array(["2022-09-01T00:20:00", "2022-09-01 00:21"], dtype="datetime64[s]"))',
    "G numpy datetime64[s] -> astype ns -> DatetimeIndex":
        'pd.DatetimeIndex(np.array(["2022-09-01T00:20:00"] * 3, dtype="datetime64[s]").astype("datetime64[ns]"))',
    "H Series ISO -> to_datetime(format=ISO8601).astype(ns) (read_clean_csv path)":
        'pd.to_datetime(pd.Series(["2022-09-01T00:20:00"] * 3), format="ISO8601").astype("datetime64[ns]")',
    "I numpy datetime64[s] from raw text with / replaced":
        'np.array([t.replace("/", "-") for t in ["2022/09/01 00:20"] * 3], dtype="datetime64[s]")',
    "J Series -> .values.astype(datetime64[m]).astype(int64) (notebook path)":
        'pd.to_datetime(pd.Series(["2022-09-01T00:20:00"] * 3), format="ISO8601").values.astype("datetime64[m]").astype("int64")',
}


def main():
    import pandas
    print("pandas", pandas.__version__, "| python", sys.version.split()[0], "| platform", sys.platform)
    for name, expr in FORMS.items():
        code = "import numpy as np, pandas as pd\nx = %s\nprint(repr(x)[:60])" % expr
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        if r.returncode == 0:
            print("OK    ", name, "->", r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")
        else:
            why = "SEGFAULT" if r.returncode < 0 or "Segmentation" in r.stderr else "ERROR"
            last = [l for l in r.stderr.strip().splitlines() if l.strip()][-1:] if r.stderr.strip() else [""]
            print("%-6s" % why, name, "->", last[0][:90])


if __name__ == "__main__":
    main()
