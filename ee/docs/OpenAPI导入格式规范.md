# 外部智能体 OpenAPI 导入:JSON 格式规范

> 版本:v1.0(2026-08-21)
> 用途:① 管理员「从 OpenAPI 导入」功能所接受的 JSON 文档格式要求;② 交给信息化系统(MES/MOM/ERP)团队的接口文档规范 —— 按此规范提供 `/v3/api-docs`(或等价)JSON,平台可自动解析出全部接口并转为智能体功能。
> 对应实现:`nodeskclaw-backend/app/services/openapi_import_service.py`(Phase 2 Task 5/6)

---

## 1. 支持范围

| 项 | 支持 | 说明 |
|---|---|---|
| OpenAPI 3.0 / 3.1 | ✅ | `parameters` + `requestBody`,`#/components/schemas/` 引用 |
| Swagger 2.0 | ✅ | `parameters` 含 `in: body/formData`,`#/definitions/` 引用 |
| 传输方式 | URL 拉取 或 直接粘贴 JSON | URL 需过 SSRF 网段白名单;仅 JSON(YAML 暂不支持) |
| 文档大小 | ≤ 5MB | 超限返回 413 |
| 拉取超时 | 15s | |
| 跨文档 `$ref`(URL 形式) | ❌ 拒绝 | 该字段跳过并给 warning |

## 2. 字段映射规范(核心)

平台解析 OpenAPI 后,每个接口(operation)变成一个"功能",入参字段按下表映射为动态表单控件:

| OpenAPI 定义 | 表单类型 | 控件 | 平台侧效果 |
|---|---|---|---|
| `type: string`(无 enum) | string | 输入框 | |
| `type: string` + `enum: [...]` | string | **下拉框** | options = enum 值 |
| `type: string` + `format: date` | string | **日期选择** | |
| `type: string` + `format: date-time` | string | 输入框 | 说明追加"ISO-8601 时间" |
| `type: string` + `format: binary` | **file** | **文件上传** | 上传中转,支持大小/扩展名白名单 |
| `type: integer / number` | number | 数字输入 | |
| `type: boolean` | boolean | 开关 | |
| `type: array / object` | object | 多行文本 | 说明标注"JSON 输入" |
| parameter 级 `required: true` | — | — | 必填(红星) |
| parameter 级 `description` | — | — | **字段作用说明**,显示在字段下方;**必填字段请务必填写 description,这是用户理解字段用途的唯一来源** |
| schema 级 `example` | — | — | 映射为**输入框占位提示**(灰字示例,如 `SO-1001`),直接告诉用户该填什么格式;不与 description 混排 |
| schema 级 `default` | — | — | **默认参数**:表单预填 + 服务端兜底;类型必须与字段类型一致(数字字段配数字,布尔配布尔) |
| `example` | — | — | 映射为输入框占位提示(见上表),不写入 description |
| path 参数(`in: path`) | 正常字段 | — | endpoint 模板 `{param}` 保留,调用时替换 |
| 嵌套对象 | 拍平 | — | 点号路径,如 `address.city` |
| 嵌套对象内 `format: binary` | file | 文件上传 | 提升为**顶层**文件字段(字段名即属性名,不走点号路径) |

**功能级映射**:

| OpenAPI 项 | 平台功能字段 |
|---|---|
| `operationId` | 功能名(自动转 snake_case;缺省用 `方法_路径`) |
| `summary` / `description` | 功能说明(用户与 AI 选择功能时的依据,建议填写) |
| `servers[0].url` + path | 调用 endpoint(**必须是绝对 http(s) 地址,相对路径会产生 warning**) |
| 200/201 响应(仅 `application/json`)为 array | 结果建议表格展示 |
| 200/201 响应 object 含数组字段 | 结果建议表格(items_path 指向该字段) |
| 其它 | 结果 JSON 展示 |

导入的 HTTP 方法范围:`get / post / put / patch / delete / head / options`。

## 3. 必须遵守的约束

1. **`servers[0].url` 必须是绝对地址**(`http(s)://host[:port]`),且该 host 需在**本组织的 SSRF 网段白名单**内(默认拒绝全部私网;管理员在组织设置 → 外部 Agent 允许网段中添加,如 `10.50.54.0/24`)
2. **禁止使用 `allOf` / `oneOf` / `anyOf` 组合式 schema** 描述请求字段 —— 平台只读 `properties`,组合式 schema 的字段不会被导入(导入预览界面会给出明确警告)。继承/复用请直接内联为完整 properties(见 §6)
3. **鉴权字段不要用 `in: header` 参数** —— header 参数会被当成普通表单字段,调用时**不会**以 HTTP header 形式发出,接口必然 401。鉴权一律由管理员在导入时统一配置(Bearer / API Key Header)
4. `default` 值类型必须匹配字段类型,**file 字段禁止配 default**;校验为**整份文档级 fail-fast** —— 任何一个字段的 default 类型不对,整份文档解析直接失败(400),而不是跳过该字段
5. 所有携带敏感语义的说明写在 `description`(它就是"字段作用")
6. 文档仅 JSON;YAML 请先转 JSON(如 Spring Boot 默认 `/v3/api-docs` 即为 JSON)
7. 导入的每个功能初始为 `draft`,试调通过后才能启用;确认导入时平台**服务端重新解析**,不信任前端回传内容

## 4. 完整示例(可直接粘贴到「贴 JSON」测试)

覆盖全部映射规则:enum 下拉+默认值、日期控件、数字默认值、布尔开关、binary 文件上传、path 参数、$ref 引用、array JSON 输入、响应数组→表格建议。

```json
{
  "openapi": "3.0.0",
  "info": { "title": "MES 缺陷管理系统", "version": "1.0.0" },
  "servers": [{ "url": "http://10.50.54.233:9000", "description": "MES 测试环境" }],
  "paths": {
    "/api/v1/defects/query": {
      "post": {
        "operationId": "queryDefects",
        "summary": "查询热压缺陷记录",
        "description": "按产线/日期/等级查询缺陷明细,可附加高级过滤与文件",
        "parameters": [
          { "name": "line", "in": "query", "required": true, "description": "产线名称",
            "schema": { "type": "string", "enum": ["热压1线", "热压2线", "热压3线"], "default": "热压1线" } },
          { "name": "start_date", "in": "query", "required": true, "description": "查询开始日期",
            "schema": { "type": "string", "format": "date" } },
          { "name": "end_date", "in": "query", "required": false, "description": "查询结束日期,缺省为今天",
            "schema": { "type": "string", "format": "date" } },
          { "name": "min_severity", "in": "query", "required": false, "description": "最低缺陷等级(1-5)",
            "schema": { "type": "integer", "default": 3 } },
          { "name": "only_open", "in": "query", "required": false, "description": "仅看未闭环缺陷",
            "schema": { "type": "boolean", "default": false } }
        ],
        "requestBody": {
          "required": false,
          "content": { "application/json": { "schema": { "$ref": "#/components/schemas/DefectFilter" } } }
        },
        "responses": {
          "200": {
            "description": "缺陷列表",
            "content": { "application/json": { "schema": { "type": "array", "items": { "$ref": "#/components/schemas/Defect" } } } }
          }
        }
      }
    },
    "/api/v1/defects/import": {
      "post": {
        "operationId": "importDefectFile",
        "summary": "批量导入缺陷明细(Excel)",
        "requestBody": {
          "required": true,
          "content": {
            "multipart/form-data": {
              "schema": {
                "type": "object",
                "properties": {
                  "file": { "type": "string", "format": "binary", "description": "明细文件(.xlsx/.csv)" },
                  "remark": { "type": "string", "description": "导入备注" }
                },
                "required": ["file"]
              }
            }
          }
        },
        "responses": {
          "200": { "description": "导入结果", "content": { "application/json": { "schema": { "type": "object", "properties": { "imported": { "type": "integer" } } } } } }
        }
      }
    },
    "/api/v1/defects/{defect_code}": {
      "get": {
        "operationId": "getDefectDetail",
        "summary": "查询单条缺陷详情",
        "parameters": [
          { "name": "defect_code", "in": "path", "required": true, "description": "缺陷编号,如 HP-2026-08-19-001",
            "schema": { "type": "string" } }
        ],
        "responses": {
          "200": { "description": "缺陷详情", "content": { "application/json": { "schema": { "$ref": "#/components/schemas/Defect" } } } }
        }
      }
    }
  },
  "components": {
    "schemas": {
      "DefectFilter": {
        "type": "object",
        "description": "高级过滤条件",
        "properties": {
          "keywords": { "type": "array", "items": { "type": "string" }, "description": "关键字,多个,JSON 数组输入" },
          "workshop": { "type": "string", "description": "车间", "default": "A区" }
        }
      },
      "Defect": {
        "type": "object",
        "properties": {
          "defect_code": { "type": "string" },
          "line": { "type": "string" },
          "severity": { "type": "integer" },
          "found_at": { "type": "string", "format": "date-time" },
          "status": { "type": "string" }
        }
      }
    }
  }
}
```

**预期解析结果**(3 个功能):

| 功能名 | 方法 路径 | 字段要点 |
|---|---|---|
| `query_defects` | POST /api/v1/defects/query | line 下拉(默认"热压1线")、start/end_date 日期、min_severity 数字(默认3)、only_open 开关、keywords JSON 文本、workshop 文本(默认"A区");结果建议表格 |
| `import_defect_file` | POST /api/v1/defects/import | file 文件上传(必填)、remark 文本 |
| `get_defect_detail` | GET /api/v1/defects/{defect_code} | defect_code 输入框(path 参数);结果建议 JSON |

## 5. Swagger 2.0 差异速查

| 项 | 2.0 写法 |
|---|---|
| 版本标识 | `"swagger": "2.0"` |
| 服务地址 | `host` + `basePath` + `schemes`(平台合成 `https://{host}{basePath}`) |
| 请求体 | `parameters` 中 `in: body`,schema 用 `#/definitions/X` 引用 |
| 文件字段 | `in: formData`, `type: file` |
| $ref 根 | `#/definitions/` 而非 `#/components/schemas/` |

## 6. 不支持与慎用清单(对接方必读)

**❌ 不支持 —— 出现即丢数据或报错,文档交付前请自检:**

| 特性 | 实际行为 | 正确做法 |
|---|---|---|
| `allOf` / `oneOf` / `anyOf` | 组合式 schema 的字段不会被导入,预览界面给出"请内联 properties"警告 | 继承/复用直接内联为完整 `properties` |
| 跨文档 `$ref`(URL 形式) | 该字段跳过并给 warning | 只用文档内 `#/components/schemas/`、`#/definitions/`、`#/components/requestBodies/` 引用 |
| file 字段配 `default` | **整份文档解析失败**(400) | 文件字段不配默认值 |
| YAML 格式 | 拒绝 | 转 JSON 后提供 |

**⚠️ 慎用 —— 不会报错,但行为可能与预期不符:**

| 特性 | 实际行为 | 建议 |
|---|---|---|
| `in: header` / `in: cookie` 参数 | 被收成普通表单字段,调用时不会以 header/cookie 发送 | 鉴权走平台统一鉴权配置;业务参数用 query/body |
| 多 content-type 的 requestBody | 按 `application/json` > `multipart/form-data` > `x-www-form-urlencoded` > 其余首个 优先级取一种 | 只声明实际需要的那一种 |
| `nullable` / `readOnly` / `writeOnly` / `deprecated` / `minLength` 等约束属性 / `collectionFormat` | 一律忽略(不报错、不生效) | 平台侧无对应校验,字段约束请在服务端自行校验 |
| 无 parameters 且无 requestBody 的接口 | 仍产出功能草稿,附"无可识别输入" warning | 正常,可导入为无参功能 |
| 枚举 `default` 不在 enum 列表内 | 原样透传,不校验 | default 取 enum 中的值 |

**✅ 其它已支持细节**:`requestBody.$ref` 指向 `#/components/requestBodies/`、path-level 与 operation-level 同名参数合并(operation 覆盖 path)、同名重复字段跳过后者并 warning。

其余映射规则与 3.x 一致。

---

*本文档与 `ee/docs/外部智能体一期方案.md`、`ee/docs/外部智能体二期方案.md` 共同构成外部智能体插件化完整规格。*
