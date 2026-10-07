# 网易云音乐每日新碟 / 0点全量扫描站

这是一个适合 iPhone / iPad Safari 的 GitHub Pages 网页，用 GitHub Actions 全量扫描网易云音乐的「全部新碟」分页，并把每天的专辑元数据保存到 `data/`。

## 重点：0点全量扫描

本项目不是“读取我关注的歌手”，而是调用网易云音乐的「全部新碟」接口，使用 `area=ALL`、`offset`、`limit` 循环分页。

每天北京时间午夜后会进入“密集扫描窗口”：

- 00:00
- 00:05
- 00:10
- 00:15
- 00:20
- 00:30
- 00:45
- 01:00

然后继续在 02:10、08:10、14:10、20:10 周期重扫今天 + 昨天。

这样做是为了尽量覆盖 00:00 附近不是一次性全部出现在新碟列表里的专辑。每次运行都会重新做全量分页，并按 `publishTime` 的北京时间日期归档到当天文件。

> 注意：GitHub Actions 的定时调度可能出现延迟，因此不能把某个工作流“00:00 准时启动”当成严格的实时保证。密集多次扫描是降低漏项的办法，但网易云接口本身如果延迟展示，仍可能需要稍后扫描。

## 网页中的“立即扫描”

网页有“立即扫描今日全部新碟”按钮。GitHub Pages 是纯静态站点，出于 GitHub API 权限和安全原因，网页不能无认证直接替你启动 GitHub Actions；点击按钮会打开该仓库的 **Actions → Update NetEase releases** 工作流页面，你只需要点击 **Run workflow**，任务就会进行一次全量扫描。

## 筛选功能

页面保留“一键过滤合集艺人”按钮，默认隐藏主要艺人为：

- `Various Artists`
- `V.A.`
- `V.A`
- `华语群星`

关闭按钮即可显示全部结果。

## 页面功能

- 全量分页扫描「全部新碟」，不按关注歌手筛选。
- 午夜密集扫描 + 白天周期扫描。
- 按日期查看历史数据。
- 搜索专辑名 / 艺人 / 公司。
- 过滤或显示 Various Artists / V.A. / 华语群星。
- 每张专辑提供网易云音乐链接。
- PWA manifest + Service Worker，可在 iPhone / iPad Safari 添加到主屏幕。
- 只保存专辑元数据，不下载音频。

## GitHub Pages 部署

1. 在 GitHub 创建一个空仓库，例如 `netease-daily-releases`。
2. 把项目全部文件上传到仓库根目录，特别是 `.github/workflows/` 不要漏掉。
3. 仓库 Settings → Pages → Build and deployment → Source 选择 **GitHub Actions**。
4. Actions 中先手动运行一次 **Update NetEase releases**。
5. 页面地址通常为：

`https://你的用户名.github.io/netease-daily-releases/`

## Safari / iPad 使用

Safari 打开网页 → 分享 → 添加到主屏幕。以后可以像 Web App 一样打开。

## 手动补抓

GitHub → Actions → Update NetEase releases → Run workflow。

可以填写：

- `date`：例如 `2026-10-07`
- `rescan_days`：例如 `2`，表示扫描指定日期以及前一天。

## 数据接口说明

网易云音乐第三方 API 的公开实现仍将 `album_new` 对应到“全部新碟”，并支持 `area=ALL` 与分页参数；本项目优先尝试公开 GET 接口，失败后再尝试 WeAPI 回退。

第三方接口不是官方稳定开发者 API，网易云音乐以后调整接口时可能需要更新抓取代码。
