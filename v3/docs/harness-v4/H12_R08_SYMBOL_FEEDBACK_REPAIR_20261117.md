# H12 R08 符号查找纠错反馈

原开发题 01 在 7 次模型调用、22 次工具调用后达到固定纠错上限，未产生补丁。
实际错误包括请求 `Option.handle_parse_result`、`Argument.handle_parse_result`、
`Option.value_from_opts` 这类在该文件中没有对应定义的限定名。原工具返回通用建议，
R03 的安全错误投影又不携带具体符号续查参数，消耗纠错额度后停止。

本修复是通用源码导航，不包含题解或私有 Judge 信息。`read_symbol` 仍用 AST、
仍不执行源码；限定名不存在时，只从已获读取授权的当前文件提供实际存在的同名
定义作为续查建议。没有唯一同名定义但限定所属类实际存在时，建议读取该类。
原请求仍返回 `HARNESS_SYMBOL_NOT_FOUND`，不会宣称已解析继承或动态属性。

安全错误投影允许携带这一种经过限定的 `retry_arguments`：工具必须是
`read_symbol`、错误码必须匹配、参数只能是相对 path 与有效限定 symbol，路径仍受
原 read roots 限制。真正下一次读取继续检查 grant、lease、epoch、保护路径和当前树。
纠错次数、模型次数、工具次数及权限上限均不增加。

仅相关定向验证：7 项 PASS，包括两项新增纠错行为、读取实际所属类、3 项
越界／可执行参数拒绝，以及原保护路径/epoch/tree 检查。原两项复现 FAIL 保留在
`evidence/harness-v4/HV4-H12-R08-SYMBOL/attempt-001`。
`attempt-002` 保留 2 PASS；一个新增测试误以为首个错误后的剩余额度是 2，实际
原策略为 3，修正测试断言后只重跑该失败项与新边界，`attempt-003` 5 PASS。
make lint、make typecheck（285 个源码文件）通过，无真实供应商调用。

源码位于独立 `hv4-h12-r08-wire-repair-20261117`，未代入原冻结候选
`04ad15c6a5ba493a7f8fb110388131229af9865d` 的真实评分。原题 01 仍为 FAIL；
离线纠错成功不等同于新候选的真实模型通过。
