"""私有图像格式互转管道（Stage: 资产工坊的 TLG → PNG 无损通道）。

设计决策（诚实边界，非遗漏）：

* **TLG5 内置解码**：纯 Python 实现（Slide LZSS + 四平面差分 + 上行还原），
  零外部依赖、可离线确定性测试。规格依据 Kirikiri 官方解码器
  （krkrz-mingw/krglhtlg LoadTLG.cpp）与 tlg-rs 实现交叉验证：
  - 头部：colors(3/4) + width/height/blockheight（i32le）+ 块大小索引表
    （blockcount × u32，krkr 读取时跳过不校验）；
  - 每块每通道：mark（0=Slide LZSS / 非 0=raw）+ size(i32le) + 数据；
    平面语义 = ``R-G / G / B-G / A`` 的 mod-256 差分；
  - Slide LZSS：窗口 4096，flag 字节 LSB-first，literal 1 字节 / match
    2 字节（pos 12bit + len nibble，0xF 扩展为 18+1 字节），缓冲含 272
    字节镜像区；LZSS 状态（text/r）跨块跨通道持续；
  - 逐像素还原：``value = plane + G``、``cl = prev_cl + value``（行内
    差分链，每行重置）、``out = upper + cl``（加上一行）—— 全部 mod 256。
* **TLG6 不内置解码**：其 GOB/ELR 熵编码复杂度完全不同（且无独立
  alpha 段），凭印象实现会得到"测试绿、真实文件全坏"的假交付 ——
  判定为不支持的变体，经外部工具链通道（GARbro 等 CLI，跨进程调用，
  严守 GPL 物理隔离红线）承接，未装配工具时给出诚实装配引导。
* **异常分型**：全部继承 EngineAdapterError（RPC 层统一映射 -32000）；
  外部工具的退出码/空产出独立分型，装配引导语义穿透到壳层。
"""
