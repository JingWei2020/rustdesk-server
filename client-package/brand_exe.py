# -*- coding: utf-8 -*-
"""把 RustDesk.exe 的应用图标替换为品牌图标（中国红 + 金色五角星）。

方法：LIEF 资源树手术。直接改 RT_ICON 各尺寸叶子的数据为品牌 ICO 中
对应尺寸的 PNG 数据，再按新长度重建 RT_GROUP_ICON 目录后写出。
（rcedit 改不了本程序的图标、ResourceHacker 在无头会话里不可靠，
 本法在 Linux 上离线完成，产物在 Windows 上用 ExtractAssociatedIcon 验真。）

用法: python3 brand_exe.py RustDesk.exe brand.ico RustDesk-branded.exe
依赖: lief==1.0.0
"""
import struct
import sys

import lief


def read_ico(path):
    data = open(path, "rb").read()
    reserved, typ, count = struct.unpack_from("<HHH", data, 0)
    assert reserved == 0 and typ == 1
    icons = {}
    for i in range(count):
        w, _h, _c, _r, _pl, _bc, size, off = struct.unpack_from("<BBBBHHII", data, 6 + i * 16)
        icons[256 if w == 0 else w] = data[off:off + size]
    return icons


RT_ICON = 3
RT_GROUP_ICON = 14


def find_child(node, type_id):
    for child in node.childs:
        if child.id == type_id:
            return child
    raise KeyError(type_id)


def main(src_exe, ico_path, out_exe):
    icons = read_ico(ico_path)
    pe = lief.parse(src_exe)
    root = pe.resources
    rt_icon = find_child(root, RT_ICON)
    rt_group = find_child(root, RT_GROUP_ICON)

    group_node = list(rt_group.childs)[0]
    group_leaf = list(group_node.childs)[0]
    gdata = bytes(group_leaf.content)
    _res, _typ, count = struct.unpack_from("<HHH", gdata, 0)
    id_to_size = {}
    for i in range(count):
        off = 6 + i * 14
        w = gdata[off]
        rid = struct.unpack_from("<H", gdata, off + 12)[0]
        id_to_size[rid] = 256 if w == 0 else w

    new_sizes = {}
    for node in rt_icon.childs:
        leaf = list(node.childs)[0]
        size = id_to_size[node.id]
        leaf.content = icons[size]  # LIEF 1.0 的 content setter 要 bytes
        new_sizes[node.id] = len(icons[size])

    g = bytearray(gdata)
    for i in range(count):
        off = 6 + i * 14
        rid = struct.unpack_from("<H", g, off + 12)[0]
        struct.pack_into("<I", g, off + 8, new_sizes[rid])
    group_leaf.content = bytes(g)

    pe.write(out_exe)
    print("written", out_exe)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
