# 00-index：维护索引

本目录是研发维护入口，不是产品源头。

- [项目结构索引](project-structure-index.md)：每个代码文件的业务职责、主要类、方法、导出和测试入口。
- [项目数据流转索引](project-dataflow-index.md)：每条 API / 后台流转的数据入口、后端链路、数据状态、外部依赖和测试入口。

使用建议：

1. 改需求前先看 `01-product/`。
2. 定位代码前看本目录。
3. 涉及页面异步、worker、TG、AI、runtime summary 时，优先查数据流转索引。
