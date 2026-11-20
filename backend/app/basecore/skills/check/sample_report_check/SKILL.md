---
name: sample_report_check
title: 样品与检测报告核查
description: 核查检测报告的 CMA/CNAS 资质、报告有效期与检测项对应关系。
category: check
version: "1.0.0"
mode: rule
level: 基础
needs: [tender, bid]
triggers:
  - 检测报告
  - 样品
  - CMA
  - CNAS
---

# 样品与检测报告核查

## 适用场景
货物类、设备类项目常要求提供第三方检测报告，资质与时效是硬门槛。

## 判定逻辑
1. 从招标文件定位检测报告/样品条款。
2. 抽资质要求（CMA、CNAS）、时效要求（近 X 年）。
3. 在投标文件中查对应资质字样。
4. 生成「检测项需逐项对应技术参数」的核对提醒。

## 输出
- 报告核查表（项 / 要求 / 投标情况）
- 缺失项与人工核对项

## 边界
报告真伪与机构资质需在发证机构官网核验。
