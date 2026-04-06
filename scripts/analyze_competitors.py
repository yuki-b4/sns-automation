"""
競合分析スクリプト
競合分析DBはGoogle Sheetsに手動入力で管理する。
このスクリプトはシートのデータ件数を確認するのみ。
"""

from sheets import get_recent_competitor_data


def main():
    data = get_recent_competitor_data()
    print(f"[競合分析] 競合分析DB 現在の件数: {len(data)}件")
    print("[競合分析] 競合データはGoogle Sheets「競合分析DB」に手動で入力してください。")


if __name__ == "__main__":
    main()
