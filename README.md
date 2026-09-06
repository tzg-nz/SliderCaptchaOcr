# SliderCaptchaOcr 滑块验证码识别

通用滑块验证码缺口识别 + 拟人拖拽轨迹生成。纯 OpenCV 图像匹配，本地运行，不需要联网打码。
并且可以生成对应可食用轨迹（基于贝赛尔曲线），以及支持本地html打开对比查看
注：缺口数量识别不是很准

## 效果演示

对照页实际运行效果（拼多多 / 极验4 样本组的缺口识别 + 轨迹预览）：

![对照页效果演示](https://github.com/tzg-nz/SliderCaptchaOcr/releases/download/v1.0.0/demo.jpg)

## 目录结构

```
SliderCaptchaOcr/
├── README.md
├── requirements.txt
└── SliderCaptchaOcr/          # 包目录
    ├── __init__.py    # 对外入口：recognize / make_trace
    ├── detector.py    # 缺口识别核心（多路算法投票）
    ├── trace.py       # 拟人拖拽轨迹生成
    ├── server.py      # 本地对照页 Web 服务
    ├── Web/           # 对照页前端（index.html / app.js / style.css）
    └── CapchaImg/     # 样本库，每组三张图 + meta.json
```

## 依赖

```bash
pip install -r requirements.txt
```

- `opencv-python`、`numpy`、`pillow`（必需）
- `ddddocr`（可选，装了作为兜底算法参与投票，不装也能跑）

## API

### recognize

**作用**：识别滑块验证码缺口位置，返回滑动距离。缺图或识别失败返回 `None`，不乱猜。

**签名**

```python
recognize(bg, block, save_local=False, tag='', out_dir=None, extra=None, group='')
```

**参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `bg` | 多格式 | 必填 | 背景图（带缺口的图） |
| `block` | 多格式 | 必填 | 滑块图（带 alpha 通道的拼图） |
| `save_local` | `bool` | `False` | 是否写本地。`True` 时背景 / 滑块 / 识别图一起落盘到 `out_dir/{tag}/`；`False` 纯识别，不写任何文件 |
| `tag` | `str` | `''` | 落盘文件名前缀 / 目录名。为空时自动按 `Card_年月日_时分秒` 命名 |
| `out_dir` | `str` | `None` | 落盘根目录，默认 `CapchaImg/` |
| `extra` | 多格式 | `None` | 可选第三张图：完整无缺口原图。提供时启用 diff 算法（原图 − 缺口图 = 洞） |
| `group` | `str` | `''` | 分组名（tag 为空时兜底用） |

`bg` / `block` / `extra` 支持以下任意格式：

- `bytes` / `bytearray`（图片原始字节）
- `base64` 字符串 / `data:image/...;base64,...` dataURL
- `http://` / `https://` 图片地址（内部自动下载）
- 本地文件路径
- `PIL.Image` 对象

**返回值**

- 成功：`int`，滑动距离 x（像素）。已换算成「滑块图左缘对齐到缺口左缘」的位移，可直接用于拖拽
- 失败：`None`。失败场景：缺背景或缺滑块、背景和滑块是同一张图 / 放反了、置信度过低

**示例**

```python
from SliderCaptchaOcr import recognize

# 纯识别，不写本地
x = recognize(bg_bytes, slider_bytes)

# 识别并留存样本（自动落盘三张图 + meta）
x = recognize(bg, block, save_local=True, tag='geetest_01')
# CapchaImg/geetest_01/
#   geetest_01_背景.png
#   geetest_01_滑块.png
#   geetest_01_识别.png   （画框标注图，识别成功才有）
#   meta.json             （x / y / n_gaps / conf / error 等结果）

# 提供完整原图，启用 diff 算法提高准确率
x = recognize(bg, block, extra=full_img)

if x is None:
    print('识别失败')
```

### make_trace

**作用**：按滑动距离生成拟人拖拽轨迹。轨迹特征：按下停顿 → 三次贝塞尔缓入缓出 → 末端过冲 2~4px 再回拉，附带手部抖动噪声。可直接喂给 Playwright / CDP 的 `dispatchMouseEvent`。

**签名**

```python
make_trace(distance, uniform=False, duration_ms=None, start=(0, 0), start_ts=0)
```

**参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `distance` | `float` | 必填 | 滑动距离（像素），一般传 `recognize` 的返回值 |
| `uniform` | `bool` | `False` | `False` 非匀速贝塞尔（拟人）；`True` 匀速直线、等间隔采样 |
| `duration_ms` | `int` | `None` | 总时长（毫秒）。默认按距离自动估算 `480 + dist * 2.4 + 随机` |
| `start` | `tuple` | `(0, 0)` | 起点 x / y（相对坐标原点） |
| `start_ts` | `int` | `0` | 起始时间戳（毫秒） |

**返回值**

`list[list]`，每个点为 `[x, y, t]`：x / y 相对 `start`，t 为累计毫秒。最后一个点精确落在目标位置。

**示例**

```python
from SliderCaptchaOcr import recognize, make_trace

x = recognize(bg, block)
if x is not None:
    pts = make_trace(x)                 # [[0, 0, 0], [0.1, 0.2, 25], ...]
    pts = make_trace(x, uniform=True)   # 匀速版
    for px, py, t in pts:
        ...  # 逐点派发鼠标事件
```

### find_gap_info（底层接口）

**作用**：`recognize` 的底层实现，返回完整识别信息（y 坐标、缺口列表、各算法候选、置信度等），适合调试或需要多缺口位置的场景。

**签名**

```python
find_gap_info(bg, block, extra=None)
```

**参数**

同 `recognize` 的 `bg` / `block` / `extra`，支持相同的多格式输入。

**返回值**

`dict` 或带 `error` 的 `dict`，主要字段：

| 字段 | 说明 |
|---|---|
| `x` / `y` | 滑动距离 / 缺口 y 坐标（识别失败为 `None`） |
| `n_gaps` | 缺口数量（按图自动判定） |
| `gaps` | 识别成功的缺口列表（失败的块不进列表）：每条 `{x, y, w, h, method, conf, kind, pad_x, pad_y, ...}`；`kind='piece'` 为滑块块对齐的缺口，`kind='hole'` 为背景上多出来的额外缺口（只计数） |
| `method` | 命中算法（如 `shadow+rim`，多路投票） |
| `conf` | 置信度 0~1 |
| `cands` | 各算法的候选位置列表 |
| `pad_x` / `pad_y` | 拼图在滑块图内的偏移 |
| `error` | 失败原因（同一张图 / 放反 / 无法识别） |

**示例**

```python
from SliderCaptchaOcr.detector import find_gap_info

info = find_gap_info(bg, block)
if info and info.get('x') is not None:
    print(info['x'], info['n_gaps'], info['method'], info['conf'])
else:
    print(info.get('error'))
```

## 识别原理（detector.py）

滑块图按 alpha 通道拆连通域，几块拼图就对应几个缺口。每块拼图在背景上用多路算法独立匹配，x / y 接近（容差 6 / 8 px）的两路以上投票通过才确认：

| 算法 | 说明 |
|---|---|
| shadow | 反色亮度 × 滑块 alpha（暗洞最亮） |
| rim | 滑块描边对背景亮边（白描边 / 浅色幽灵缺口） |
| outline | alpha 描边对背景 Canny 边缘 |
| dark | 剪影窗口平均亮度（只作参考，不投票） |
| ddddocr | 可选兜底（不投票） |
| diff | 提供完整原图（`extra`）时，与缺口背景做差找洞 |
| hole | 额外缺口计数：亮边 / 暗剪影 / 描边 / 覆盖块至少两路同时认可才计数，不画框、不参与滑动距离 |

滑动距离只用滑块块对齐的结果，背景上多出来的洞只计数。

## 对照页（server.py）

本地 Web 界面，可视化浏览 / 上传 / 识别 `CapchaImg/` 里的样本。

在仓库根目录（`README.md` 所在层）运行：

```bash
python -m SliderCaptchaOcr                          # 默认 127.0.0.1:8765，自动开浏览器
python -m SliderCaptchaOcr --port 9000 --no-browser # 指定端口，不开浏览器
python -m SliderCaptchaOcr --dir D:/some/dir        # 指定图片根目录
```

功能：上传背景 / 滑块（可只传一张）、点「识别」出标注图、生成轨迹预览、重命名 / 删除样本组。API 端点：`/api/cases`、`/api/save`、`/api/recognize`、`/api/trace`、`/api/fetch`、`/api/delete`、`/api/rename`。
