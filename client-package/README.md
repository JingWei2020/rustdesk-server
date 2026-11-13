# 定制客户端打包（路线图 #12）

把官方 RustDesk 客户端安装器打成「装完即连自建服务器」的单文件包：
免改设置，ID 服务器 / 中继 / API / 公钥四项全部预置。

## 原理（已实测的约束）

- 官方安装器只认文件名注入，且当前版本只注入 host+key 两项
  （`rustdesk-host=<host>,key=<key>,.exe`，注意 key 末尾的 `=` 需要尾随逗号保护）；
  中继与 API 地址没有文件名注入通道；
- 编译期环境变量注入在当前客户端源码里已被移除，写死配置需 fork 客户端
  改 `hbb_common` 常量（且 API 地址无常量可写）——不走这条路；
- 因此用 Windows 自带 IExpress 把三件套打成一个自解压 exe：官方安装器
  （magic 文件名）+ `setup.cmd`（装完调 PowerShell 写配置）+ `setcfg.ps1`
  （把四项配置写入所有用户的 RustDesk2.toml 并重启服务）。

## 重新打包（Windows 上）

1. 下载官方 1.5.3 安装器，改名为 `rustdesk-host=<你的ID服务器host>,key=<你的公钥>,.exe`
   放到本目录，与 `setup.cmd`、`setcfg.ps1` 同级；
2. 在 `setcfg.ps1` 里核对四项服务器参数；
3. 运行 `build.sed`（IExpress 工程文件）生成单文件包。

产物即交付给使用者的安装包，双击安装即可。

## 在线分发

控制台网关（仓库 `gate/`）在完成双因素登录后提供 `/gate/client` 页：
展示当前服务器参数、提供本包下载、附官方安装器的 magic 文件名自助定制法。
