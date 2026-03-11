#!/usr/bin/env python3
"""生成关注对象导入模版 Excel"""
from openpyxl import Workbook
from pathlib import Path

wb = Workbook()
ws = wb.active
ws.title = "关注对象"
ws.append(["平台", "ID", "昵称"])
ws.append(["抖音", "示例ID1", "示例昵称1"])
ws.append(["快手", "示例ID2", ""])
wb.save(Path(__file__).parent / "关注对象导入模版.xlsx")
print("已生成: 关注对象导入模版.xlsx")
