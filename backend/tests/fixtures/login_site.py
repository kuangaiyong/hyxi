"""带登录门的本地 fixture 站点。

用来验证 facebook_group.js 的登录 / 会话复用 / 两步验证退出路径。真 HTTP、真表单、
真 Set-Cookie、真重定向，选择器与 facebook_group.js 里那一套逐字一致 —— 换句话说
它验的是脚本真实会用的选择器，不是另写一套好过的。

三个账号对应三条分支：
  ok@example.com  / 正确密码 → 登录成功
  2fa@example.com / 任意密码 → 两步验证页（脚本应以退出码 3 交回给人）
  其余                        → 登录失败（#error_box）
"""

import base64
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote

# 1×1 PNG。图片必须真的返回 —— 404 的话浏览器按 alt 文本渲染，尺寸过滤就测不成了
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

GOOD_USER = "ok@example.com"
GOOD_PASSWORD = "correct-horse-battery"
TWO_FACTOR_USER = "2fa@example.com"
SESSION_COOKIE = "fixture_session"

# 表单结构对齐 2026-08-03 探测到的真实 facebook.com：
#   - 输入框 id 是随机的，只能按 name 选
#   - input[type=submit] 是 0×0 不可见的
#   - 表单里 DOM 顺序第一个 [role=button] 是「显示密码」图标，点它只会把密码显示出来
#   - 页面上没有 [data-testid]，也没有 [name="login"]
# 所以采集器靠在密码框按回车提交；这份 fixture 复刻同样的陷阱，保证测的是真实路径。
_LOGIN_PAGE = """<html><head><meta charset="utf-8"><title>登录</title></head><body>
<form method="POST" action="/login" id="login_form">
  <div role="button" style="width:24px;height:24px" onclick="document.getElementById('_R_1hmkqsqppb6amH1_').type='text'"></div>
  <input type="text" name="email" id="_R_1h6kqsqppb6amH1_" />
  <input type="password" name="pass" id="_R_1hmkqsqppb6amH1_" />
  <input type="submit" value="登录" style="width:0;height:0;position:absolute;left:-9999px" />
</form>
{error}
</body></html>"""

_ERROR_BOX = '<div id="error_box">账号或密码不正确</div>'

_TWO_FACTOR_PAGE = """<html><head><meta charset="utf-8"><title>两步验证</title></head><body>
<form method="POST" action="/checkpoint/2fa">
  <input type="text" name="approvals_code" id="approvals_code" />
  <button type="submit">提交</button>
</form>
</body></html>"""

# 注册页：真站登录表单下方那个绿色大按钮指向 /reg/?entry_point=login&next=...，
# 人工授权时误点一下就落到这里。页面上没有 email/pass 输入框，也没有 feed，
# 于是轮询循环两头都不认，会一声不吭地空转到超时 —— 这份 fixture 就是复现那一幕。
_REG_PAGE = """<html><head><meta charset="utf-8"><title>注册</title></head><body>
<form method="POST" action="/reg/">
  <input type="text" name="reg_email__" />
</form>
<a href="/login/">登录</a>
</body></html>"""

# 小组页结构对齐 2026-08-04 探测到的真实（已登录）小组页：
#   - 没有 abbr[data-utime]，也没有 data-post-id / data-comment-id，
#     帖子和评论的 id 只能从固定链接的 URL 里取
#   - 没有 h3 / strong，作者只剩 a[href*="/user/"]，而同一个人会连出几个这样的链接，
#     排在前面的是头像、文本为空 —— 直接 querySelector 取到的是空的那个
#   - 评论正文是第一个 div[dir=auto]，没有专用容器
#   - 主贴时间链接的 aria-label 是**相对时间**，评论的是绝对时间但走 Facebook 账号
#     自己的时区（实测比本地早 15 小时）—— 两个坑都复刻在这里，绝对的本地时间
#     只有 hover 出来的 tooltip 有
#   - 长正文只渲染前几行，末尾挂一个 role=button 的「展开」。不点它，textContent
#     拿到的是残缺正文 + 「展开」两个字（实测有一条整条正文只剩 16 个字符）；
#     点开后按钮文字变成「收起」，同样会被 textContent 吃进正文。最后一条主贴复刻这一幕。
#   - 信息流里混着不是帖子的 article（广告 / 推荐小组卡片），既没有固定链接也没有
#     正文容器。中间那条 role=article 就是它，提取器必须把它丢掉而不是存成空帖。
#   - **正文图是 <img>、host 在 scontent 上、渲染尺寸几百像素**；界面图标是
#     data:image/svg+xml、emoji 在 static.xx.fbcdn.net，而**头像是 <svg><image>
#     不是 img**。第一条主贴把这四种都摆上了：只有 scontent 上那张 400×300 该被抓走。
#   - **评论的多段正文是一串并列的 div[dir=auto]，一段一个**（实测一条评论有 9 段），
#     querySelector 只拿第一段。第一条评论复刻成 3 段，并在它里面再嵌一条回复 ——
#     嵌套回复也是 article，取多段时必须限定在本条评论这一层，否则会把子回复的
#     正文吞进父评论。
#   - **评论区自己还有两处折叠，和正文那个「展开」是两回事**：首屏每条主贴只渲染前
#     两三条评论，其余藏在「查看更多评论」后面（还分页，点一次只多出一页）；一条评论
#     底下的嵌套回复另有一个「查看 N 条回复」。真实库实测每主贴回复数分布
#     {1条:17, 2条:30, 3条:5}，上限死死卡在 3、79 条主贴一条都没超过 —— 就是这两处
#     从来没被点开（用户报「只采到 3 条、实际应有 4 条」）。第一条主贴把两者都摆上，
#     「查看更多评论」故意分两页，用来钉住「必须循环点到不再增长」而不是点一次就算完。
#     按钮文字里带条数，**不能像正文「展开」那样精确匹配**。
#     按钮一律不是 div[dir=auto]：真实库里 171 条正文没有一条带 UI 文案尾巴，
#     说明评论正文提取压根碰不到这些按钮，复刻时别把它们做成 dir=auto。
_FEED_PAGE = """<html><head><meta charset="utf-8"><title>小组</title></head><body>
__STALE_DIALOG__
<!-- 9001 卡片上评论数「5」= 5501~5505 全部（真站那个数字含回复）。它的折叠是就地展开的：
     v1.11.2 真站跑出过就地展开，所以这条路和浮层那条都得在 -->
<div role="feed">
  <div role="article">
    <a href="/groups/2407063016436085/user/11/" aria-label="Marieke_V"
       ><svg width="40" height="40"><image href="/media/scontent/avatar11.png"
         width="40" height="40" /></svg></a>
    <a href="/groups/2407063016436085/user/11/">Marieke_V</a>
    <a href="/groups/2407063016436085/posts/9001/" aria-label="6天"
       data-tip="2026年5月28日周三17:54"><span>6天</span></a>
    <div data-ad-comet-preview="message">Na drie maanden met de HYXi Halo ben ik echt tevreden.</div>
    <img src="/media/scontent/halo-installatie.png" width="400" height="300"
         alt="可能是包含下列内容的图片：热水器" />
    <img src="/media/scontent/emoji.png" width="16" height="16" alt="👍" />
    <img src="/media/static/banner.png" width="400" height="300" alt="站点横幅" />
    <div role="button" tabindex="0" aria-label="发表评论">5</div>
    <div role="article">
      <a href="/groups/2407063016436085/user/22/" aria-label="Joost1988"></a>
      <a href="/groups/2407063016436085/user/22/">Joost1988</a>
      <div dir="auto">Zelfde ervaring hier, +1</div>
      <div dir="auto">Vooral de app is sterk verbeterd.</div>
      <div dir="auto">Alleen de min SOC blijft een raadsel.</div>
      <a href="/groups/2407063016436085/posts/9001/?comment_id=5501"
         aria-label="2026年5月28日凌晨3:42" data-tip="2026年5月28日周三18:42">6天</a>
      <div role="article">
        <a href="/groups/2407063016436085/user/44/" aria-label="Sanne_K"></a>
        <a href="/groups/2407063016436085/user/44/">Sanne_K</a>
        <div dir="auto">Die 8% ondergrens is inderdaad vreemd.</div>
        <a href="/groups/2407063016436085/posts/9001/?comment_id=5502"
           aria-label="2026年5月28日凌晨4:10" data-tip="2026年5月28日周三19:10">6天</a>
      </div>
      <div id="replyfold" role="button" tabindex="0" onclick="moreReplies()"
        >查看 1 条回复</div>
    </div>
    <div id="cmtfold" role="button" tabindex="0" onclick="moreComments()"
      >查看更多评论</div>
  </div>
  <div role="article">
    <div>Gesponsord</div>
  </div>
  <!-- 2026-09-13 真站只读探查复刻（主贴 2534929036982815）：卡片上评论数按钮文字「6」、
       aria-label「发表评论」，**含回复**；只显示 1 条评论；折叠叫「查看更多**回答**」。
       点它 = pushState 到 /permalink/<id>/ + 弹 [role=dialog]，浮层内容走 XHR 加载；
       点浮层「关闭」回到信息流，卡片还在、没有重载。
       真站上 v1.11.0~v1.11.3 就是没退出这个浮层，整轮只剩一条帖子；v1.11.4 退出来了
       却把这条主贴整轮拉黑，隐藏的评论每一轮都采不到 -->
  <div role="article">
    <a href="/groups/2407063016436085/user/99/" aria-label="Sofie_M"></a>
    <a href="/groups/2407063016436085/user/99/">Sofie_M</a>
    <a href="/groups/2407063016436085/posts/9004/" aria-label="3天"
       data-tip="2026年5月31日周日11:20"><span>3天</span></a>
    <div data-ad-comet-preview="message">Iemand ervaring met de garantie-afhandeling?</div>
    <div role="button" tabindex="0" aria-label="发表评论">__COUNT_9004__</div>
    <div role="button" tabindex="0" onclick="openThread('9004')">查看更多回答</div>
    __CARD_9004__
  </div>
  <div role="article">
    <a href="/groups/2407063016436085/user/33/" aria-label="TechNerd_NL"></a>
    <a href="/groups/2407063016436085/user/33/">TechNerd_NL</a>
    <a href="/groups/2407063016436085/posts/9002/" aria-label="4天"
       data-tip="2026年5月30日周五05:27"><span>4天</span></a>
    <div data-ad-comet-preview="message" id="folded"><span>Firmware 2.4.1 heeft…
      <div role="button" tabindex="0" onclick="unfold()">展开</div></span></div>
  </div>
  <!-- 评论数对不上、卡片上却**没有任何折叠按钮**：信息流里没有入口可点，只能等本轮信息流
       滚完后按固定链接打开补齐（固定链接整页加载时帖子同样显示在浮层里，2026-09-13 实测） -->
  <div role="article">
    <a href="/groups/2407063016436085/user/61/" aria-label="Pieter_V"></a>
    <a href="/groups/2407063016436085/user/61/">Pieter_V</a>
    <a href="/groups/2407063016436085/posts/9005/" aria-label="5天"
       data-tip="2026年5月29日周五08:15"><span>5天</span></a>
    <div data-ad-comet-preview="message">Werkt de noodstroomfunctie ook zonder internet?</div>
    <div role="button" tabindex="0" aria-label="发表评论">__COUNT_9005__</div>
    __CARD_9005__
  </div>
  <!-- 折叠点开的却是**别的帖子**的浮层（地址是 /permalink/9008/，信息流里没有那条帖子）。
       真站没见过，但把别的帖子的评论挂到这条下面，比少采几条糟得多（用户报过「主贴 A 的回复贴
       不是他的」）：对不上就不收。**别拿信息流里有的帖子当那个「别的帖子」**：它自己的整串随后会被
       补齐并纠正层级，挂错的被顺手改回来，断言就钉不住这条防线（实测踩过） -->
  <div role="article">
    <a href="/groups/2407063016436085/user/73/" aria-label="Ingrid_S"></a>
    <a href="/groups/2407063016436085/user/73/">Ingrid_S</a>
    <a href="/groups/2407063016436085/posts/9007/" aria-label="5天"
       data-tip="2026年5月29日周五12:40"><span>5天</span></a>
    <div data-ad-comet-preview="message">Welke app gebruiken jullie voor de monitoring?</div>
    <div role="button" tabindex="0" onclick="openThread('9008')">查看更多评论</div>
  </div>
  <!-- 折叠按钮一点就**整页跳走**（不是浮层）。真站没见过这种，但点完不检查的后果是整轮
       报废（v1.11.0 的教训），所以退回信息流这条路要一直有人守着 -->
  <div role="article">
    <a href="/groups/2407063016436085/user/71/" aria-label="Kees_B"></a>
    <a href="/groups/2407063016436085/user/71/">Kees_B</a>
    <a href="/groups/2407063016436085/posts/9006/" aria-label="4天"
       data-tip="2026年5月30日周六14:05"><span>4天</span></a>
    <div data-ad-comet-preview="message">Ervaringen met de garantie via de webshop?</div>
    <div role="button" tabindex="0"
      onclick="location.href='/groups/2407063016436085/posts/9006/'">查看更多评论</div>
  </div>
  __DEAD_FOLD_CARD__
  __GHOST_CARD__
  <!-- 撑高页面：真站的信息流很长，滚到底才会触发懒加载。两条都要满足：
       ① 页面不够高的话 window.scrollTo() 压根不产生 scroll 事件；
       ② **必须高过 humanRead() 能滚到的距离**（每批开头 2~4 次、每次 300~900px，
       最多 3600px）—— 否则每批一开头就滚到底了，懒加载的内容会在提取过程中插进来，
       scrollOnce() 反而量不出增长，测试就变成对旧代码也成立的空转（实测踩过） -->
  <div id="spacer" style="height:8000px"></div>
</div>
<script>
function unfold() {
  document.getElementById('folded').innerHTML =
    '<span>Firmware 2.4.1 heeft bij mij de WiFi-verbinding gesloopt. '
    + 'Na een downgrade werkt alles weer. '
    + '<div role="button" tabindex="0">收起</div></span>';
}
// 评论区的折叠。**分两页**：点一次只多出一页评论，按钮还留着 —— 采集器必须循环点
// 到不再增长，点一次就收工照样漏。真站上的「查看更多评论」就是这个行为。
var cmtPage = 0;
// qs 缺省是「评论自带 id」的形态；嵌套回复可以传 comment_id=父&reply_comment_id=自己
function newComment(id, uid, name, body, hhmm, qs) {
  return '<div role="article">'
    + '<a href="/groups/2407063016436085/user/' + uid + '/" aria-label="' + name + '"></a>'
    + '<a href="/groups/2407063016436085/user/' + uid + '/">' + name + '</a>'
    + '<div dir="auto">' + body + '</div>'
    + '<a href="/groups/2407063016436085/posts/9001/?' + (qs || 'comment_id=' + id) + '"'
    + ' data-tip="2026年5月28日周三' + hhmm + '">6天</a>'
    + '</div>';
}
// 5503 的正文**自己也折叠着**：长评论在真站上同样只渲染前几行、末尾挂一个「展开」
// （真实库最长的回复 815 字，结尾全都完整，说明真站评论确实带这个按钮、靠
// expandBodies 点开）。它是被「查看更多评论」**加载出来**的 —— 先点正文「展开」
// 再点评论折叠的话，它出现时那一轮已经点完了，残文就这么入库。
// 可见部分故意超过 100 字：指纹只吃正文前 100 字，截断版和完整版于是算出**同一个
// 指纹**，下一批就算展开了也会被当成已见过丢掉，残文永远修不回来
var LONG_5503 = 'Bij mij hangt hij aan een Shelly 3EM, werkt prima. De P1-koppeling via de '
  + 'HomeWizard gaf de eerste week storingen, maar sinds firmware 2.4.3 is dat helemaal '
  + 'opgelost en laadt hij netjes op zonne-overschot. Alleen de app blijft traag bij het '
  + 'wisselen tussen de tabbladen.';
function unfoldComment(btn) {
  btn.parentElement.innerHTML = LONG_5503 + ' <div role="button" tabindex="0">收起</div>';
}
function moreComments() {
  var fold = document.getElementById('cmtfold');
  cmtPage++;
  if (cmtPage === 1) {
    fold.insertAdjacentHTML('beforebegin', newComment(
      '5503', '55', 'Bram_H', LONG_5503.slice(0, 110)
        + '… <div role="button" tabindex="0" onclick="unfoldComment(this)">展开</div>', '19:55'));
  } else {
    fold.insertAdjacentHTML('beforebegin', newComment(
      '5505', '66', 'Lieke_dV', 'Let op de firmware, 2.4.1 gaf hier problemen.', '20:31'));
    fold.remove();
  }
}
// 5504 的链接是 ?comment_id=<父评论 5501>&reply_comment_id=<自己>。这是 Facebook 嵌套回复
// 固定链接的常见形态，**本机未在真站核实**（这台机器不访问 Facebook）；5502 保留
// 「自带 id」的形态，两种都要认。只认 comment_id 的话，回复拿到的是父评论的 id ——
// message_id 撞车，入库时按 id 归并，父评论的作者和正文被回复覆盖；时间锚点的标记也
// 由 id 派生，回复连时间都继承了父评论的
// 信息流是懒加载的：滚到底之后，下一批要过一会儿才插进来。**真站上比 2.5 秒慢** ——
// 实测一轮只提取到 8 条就宣布「页面不再增长，已到底」，而那个小组有一百多条帖子，
// 于是每次采集都只看得到首屏。这里故意让它 4 秒后才出现，钉住「要等，别急着判到底」
var lazyArmed = false;
window.addEventListener('scroll', function () {
  // **只有真滚到底才触发**，和真站一致。挂在任意一次 scroll 上是不对的：
  // 每批开头的 humanRead() 会先滚几下，那样新内容会在提取过程中就插进来，
  // 等 scrollOnce() 去量高度时它已经在里面了 —— 反而量不出「增长」
  var atBottom = window.scrollY + window.innerHeight >= document.body.scrollHeight - 50;
  if (lazyArmed || !atBottom) return;
  lazyArmed = true;
  setTimeout(function () {
    document.querySelector('[role="feed"]').insertAdjacentHTML('beforeend',
      '<div role="article">'
      + '<a href="/groups/2407063016436085/user/88/" aria-label="Wouter_L"></a>'
      + '<a href="/groups/2407063016436085/user/88/">Wouter_L</a>'
      + '<a href="/groups/2407063016436085/posts/9003/" aria-label="2天"'
      + ' data-tip="2026年6月1日周一09:12"><span>2天</span></a>'
      + '<div data-ad-comet-preview="message">Tweede scherm vol, dit kwam pas na het scrollen.</div>'
      + '</div>');
  }, 4000);
});
function moreReplies() {
  var fold = document.getElementById('replyfold');
  fold.insertAdjacentHTML('beforebegin', newComment(
    '5504', '77', 'Ruud_T', 'Die ondergrens is instelbaar in de app.', '19:22',
    'comment_id=5501&reply_comment_id=5504'));
  fold.remove();
}
</script>
__THREAD_SCRIPT__
__TIP_SCRIPT__
</body></html>"""

# 一刻不停在导航的页面。人在窗口里输账号、提交、过验证，每一步都是一次导航，
# 而轮询每 2 秒查一次 loggedIn —— 两者撞上时 Playwright 会抛「Execution context was
# destroyed」。这页把那个窗口放到最大，用来钉死「轮询不能因为导航而把脚本搞挂」。
# 导航挂在 load 上而不是解析期，否则 gotoPage 自己就会被打断，测的就不是轮询了。
# 帖子详情页（9006 的折叠整页跳过来）。**信息流不在这里** —— 采集器要是留在这儿，
# 这一批就只提取得到这一条帖子，滚动也没有下一批。5801 只在这一页上有
_DETAIL_PAGE = """<html><head><meta charset="utf-8"><title>帖子</title></head><body>
<div role="article">
  <a href="/groups/2407063016436085/user/71/" aria-label="Kees_B"></a>
  <a href="/groups/2407063016436085/user/71/">Kees_B</a>
  <a href="/groups/2407063016436085/posts/9006/" data-tip="2026年5月30日周六14:05">4天</a>
  <div data-ad-comet-preview="message">Ervaringen met de garantie via de webshop?</div>
  <div role="article">
    <a href="/groups/2407063016436085/user/72/">Lotte_P</a>
    <div dir="auto">Via de webshop ging het bij mij binnen een week.</div>
    <a href="/groups/2407063016436085/posts/9006/?comment_id=5801"
       data-tip="2026年5月30日周六15:10">4天</a>
  </div>
</div>
</body></html>"""

_POST_RE = re.compile(r"^/groups/\d+/posts/\d+/?$")

# ===== 帖子浮层（2026-09-13 真站只读探查复刻，主贴 2534929036982815）=====
#
# 真站实测到的、下面逐条复刻的：
#   - 点「查看更多回答」= pushState 到 /groups/<gid>/permalink/<id>/ + 弹 [role=dialog]；
#     浮层内容是 XHR 取回来的（这里走 /api/thread/<id>，服务器记下被请求几次）
#   - 浮层**盖住信息流**，信息流的 DOM 还在后面。浮层开着时信息流里的元素 hover 不到
#   - 浮层里的主贴头部**没有** /posts/<id> 链接
#   - 评论与回复**全是主贴 article 下的兄弟节点**，回复不嵌在父评论 article 里；层级只体现在
#     缩进上（真站 418 / 479 / 521px，这里取相对值 0 / 40 / 80）
#   - 评论的链接 comment_id=<自己>；回复**不论第几层**都是 comment_id=<顶层评论>&reply_comment_id=<自己>
#   - 点浮层「关闭」（aria-label）后 URL 回到信息流、卡片还在、没有重载
#   - 直接打开固定链接是**首页信息流上盖一层同样的浮层**，背景里是不相干的帖子
#   - 评论排序按钮默认「最相关」（账号级设置，采集器不许点）
#
# 没在真站见过、为了守住退路而加的（**是假设，不是实测**）：
#   - Esc 与后退也能关浮层；sticky_dialog=True 时三种都关不掉，只有重新载入信息流才行
#   - 浮层里还有一处「查看更多回复」
#   - 9005 里 5704 的链接说它回的是 5701，位置却排在 5703 后面、缩进和回复一样
_GID = "2407063016436085"


def _comment(pid, cid, uid, name, body, tip, top=None, image=None):
    """一条评论 / 回复的 article。给了 top 就是回复。"""
    qs = f"comment_id={top}&reply_comment_id={cid}" if top else f"comment_id={cid}"
    img = (f'<img src="/media/scontent/{image}" width="300" height="200" alt="图片">'
           if image else "")
    return (
        '<div role="article">'
        f'<a href="/groups/{_GID}/user/{uid}/" aria-label="{name}"></a>'
        f'<a href="/groups/{_GID}/user/{uid}/">{name}</a>'
        f'<div dir="auto">{body}</div>{img}'
        f'<a href="/groups/{_GID}/posts/{pid}/?{qs}" data-tip="{tip}">3天</a>'
        '</div>'
    )


# 卡片上露出来的那一条。真站那条主贴也是：卡片只显示排在浮层**最后**的那条评论
_CARD_COMMENTS = {
    "9004": _comment("9004", "5606", "12", "Bart_K",
                     "De accu moet eerst volledig opladen, daarna kun je de modus wijzigen.",
                     "2026年5月31日周日12:10"),
    "9005": _comment("9005", "5701", "81", "Anouk_R",
                     "Ja, de noodstroom werkt volledig lokaal.", "2026年5月29日周五09:02"),
}

# 9004 浮层里的「查看更多回复」。stale_dialog_fold=True 时点了原样换回自己、一条都不多
_MORE_FOLD_9004 = (40, '<div role="button" tabindex="0" data-more="9004"'
                       ' onclick="moreThreadReplies(this)">查看更多回复</div>')

# 浮层里的评论区：(缩进 px, html)，按文档顺序
_THREADS = {
    # 期望入库：5601 L1 → 5602 L2 → 5603、5604 L3（挂 5602）→ 5605 L2（挂 5601）→ 5606 L1
    "9004": {
        "author": ("99", "Sofie_M"),
        "body": "Iemand ervaring met de garantie-afhandeling?",
        "count": 6,
        "items": [
            (0, _comment("9004", "5601", "12", "Bart_K", "Bij mij duurde het drie weken.",
                         "2026年5月31日周日12:02", image="garantie-mail.png")),
            (40, _comment("9004", "5602", "99", "Sofie_M",
                          "Drie weken? Bij mij is het al vijf weken stil.",
                          "2026年5月31日周日12:30", top="5601")),
            (80, _comment("9004", "5603", "12", "Bart_K", "Bel ze, mailen helpt niet.",
                          "2026年5月31日周日12:41", top="5601")),
            (80, _comment("9004", "5604", "12", "Bart_K", "En vraag meteen naar het RMA-nummer.",
                          "2026年5月31日周日12:44", top="5601")),
            _MORE_FOLD_9004,
            (0, _CARD_COMMENTS["9004"]),
        ],
    },
    # 卡片上没有折叠按钮，只能按固定链接补齐。期望入库：5701 L1 → 5702 L2 → 5703 L1 →
    # 5704 L2 **挂 5701**（按缩进会挂到 5703 下，链接说的才算数）
    "9005": {
        "author": ("61", "Pieter_V"),
        "body": "Werkt de noodstroomfunctie ook zonder internet?",
        "count": 4,
        "items": [
            (0, _CARD_COMMENTS["9005"]),
            (40, _comment("9005", "5702", "61", "Pieter_V", "Ook als de router uit staat?",
                          "2026年5月29日周五09:20", top="5701")),
            (0, _comment("9005", "5703", "82", "Joris_W",
                         "Bij mij schakelt hij binnen een seconde om.", "2026年5月29日周五09:45")),
            (40, _comment("9005", "5704", "81", "Anouk_R", "Ja, ook zonder router.",
                          "2026年5月29日周五10:02", top="5701")),
        ],
    },
    # 原帖显示 1 条评论，卡片上、浮层里却一条都看不到（删了没减数、被「最相关」藏掉）。
    # 只在 ghost_comment_card=True 时出现在信息流里
    "9011": {
        "author": ("77", "Daan_V"),
        "body": "Is er al een firmware-update voor de Halo 6kW?",
        "count": 1,
        "items": [],
    },
    # 信息流里没有这条帖子，只有 9007 的折叠会错开到它的浮层。一条都不许被收进来
    "9008": {
        "author": ("74", "Femke_D"),
        "body": "Hoe zit het met de terugleververgoeding?",
        "count": 2,
        "items": [
            (0, _comment("9008", "5901", "75", "Tom_H", "Geen idee, bij mij staat die op nul.",
                         "2026年5月28日周四10:05")),
            (40, _comment("9008", "5902", "74", "Femke_D", "Dank je, ik ga het navragen.",
                          "2026年5月28日周四10:30", top="5901")),
        ],
    },
}

# 浮层里「查看更多回复」加载出来的
_MORE_REPLIES = {
    "9004": (40, _comment("9004", "5605", "99", "Sofie_M", "Top, bedankt voor de tip!",
                          "2026年5月31日周日13:05", top="5601")),
}


def _row(px, html):
    return f'<div style="margin-left:{px}px">{html}</div>'


def _thread_items(pid, site):
    html = "".join(_row(px, h) for px, h in _THREADS[pid]["items"])
    # tooltip 出不来的回复：5603 的链接上没有 data-tip
    return html.replace(' data-tip="2026年5月31日周日12:41"', "") if site.tipless_reply else html


def _thread_dialog(pid, site, deferred=False):
    """site 是 LoginSite（原帖数多出几条、回复取不取得到时间都从它身上读）；
    deferred：评论区先空着，由页面随后另取（见 LoginSite.deferred_thread_comments）"""
    t = _THREADS[pid]
    uid, name = t["author"]
    count = t["count"] + (site.hidden_comments if pid in ("9004", "9005") else 0)
    return (
        '<div role="dialog" id="thread-dialog" style="position:fixed;left:0;top:0;right:0;'
        'bottom:0;overflow:auto;background:#fff;z-index:10">'
        '<div role="button" tabindex="0" aria-label="关闭" onclick="closeThread(\'button\')">✕</div>'
        '<div role="article">'
        f'<a href="/groups/{_GID}/user/{uid}/" aria-label="{name}"></a>'
        f'<a href="/groups/{_GID}/user/{uid}/">{name}</a><span>3天</span>'
        f'<div data-ad-comet-preview="message">{t["body"]}</div>'
        f'<div role="button" tabindex="0" aria-label="发表评论">{count}</div>'
        '<div role="button" tabindex="0">最相关</div>'
        + ("" if deferred else _thread_items(pid, site))
        + '</div></div>'
    )


_THREAD_SCRIPT = """<script>
var STICKY = __STICKY__;
var DEFERRED = __DEFERRED__;
// 9004 的浮层里一查 article 就抛错：稳定复现「收割浮层时页面调用失败」
// （真站上可能是浮层里点了什么触发整页跳转、页面上下文被销毁，时机抓不准）
if (__BROKEN__) {
  var qsa = Element.prototype.querySelectorAll;
  Element.prototype.querySelectorAll = function (s) {
    if (this.id === 'thread-dialog' && location.pathname.indexOf('/9004/') >= 0
        && String(s).indexOf('article') >= 0) throw new Error('fixture: 浮层里的页面调用失败');
    return qsa.call(this, s);
  };
}
function openThread(pid) {
  if (document.getElementById('thread-dialog')) return;
  history.pushState({ thread: pid }, '', '/groups/2407063016436085/permalink/' + pid + '__THREAD_URL_TAIL__');
  fetch('/api/thread/' + pid).then(function (r) { return r.text(); }).then(function (html) {
    document.body.insertAdjacentHTML('beforeend', html);
    if (DEFERRED <= 0) return;
    setTimeout(function () {
      fetch('/api/thread/' + pid + '/items').then(function (r) { return r.text(); }).then(function (rows) {
        var root = document.querySelector('#thread-dialog [role="article"]');
        if (root) root.insertAdjacentHTML('beforeend', rows);
      });
    }, DEFERRED);
  });
}
// 点了只让信息流多出一条（懒加载插进来的），这条主贴自己的评论一条不多
function deadFold() {
  fetch('/api/fold/9010');
  document.querySelector('[role="feed"]').insertAdjacentHTML('beforeend', '<div role="article"></div>');
}
function dropThread() {
  var d = document.getElementById('thread-dialog');
  if (d) d.remove();
}
function closeThread(how) {
  fetch('/api/close/' + how);
  if (STICKY) return;
  dropThread();
  if (history.state && history.state.thread) history.back();
}
window.addEventListener('popstate', function () { if (!STICKY) dropThread(); });
document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeThread('esc'); });
function moreThreadReplies(btn) {
  fetch('/api/thread/' + btn.getAttribute('data-more') + '/more')
    .then(function (r) { return r.text(); })
    .then(function (html) { btn.parentElement.outerHTML = html; });
}
</script>"""

_TIP_SCRIPT = """<script>
document.addEventListener('mouseover', function (e) {
  var a = e.target.closest && e.target.closest('a[data-tip]');
  if (!a || document.getElementById('tip')) return;
  var tip = document.createElement('div');
  tip.id = 'tip';
  tip.setAttribute('role', 'tooltip');
  tip.textContent = a.getAttribute('data-tip');
  document.body.appendChild(tip);
});
document.addEventListener('mouseout', function () {
  var t = document.getElementById('tip');
  if (t) t.remove();
});
</script>"""

# 背景里那条是**别的小组**的帖子：补齐时只能从浮层里取，把它收进来就是一条不相干的脏数据
_PERMALINK_PAGE = """<html><head><meta charset="utf-8"><title>帖子</title></head><body>
<div role="feed">
  <div role="article">
    <a href="/groups/5550001/user/91/" aria-label="Mariana_S"></a>
    <a href="/groups/5550001/user/91/">Mariana_S</a>
    <a href="/groups/5550001/posts/9999/" data-tip="2026年6月1日周一10:00"><span>1天</span></a>
    <div data-ad-comet-preview="message">Olá pessoal, alguém tem o inversor híbrido?</div>
    <div role="button" tabindex="0" aria-label="发表评论">1</div>
    <div role="article">
      <a href="/groups/5550001/user/92/">Tiago_R</a>
      <div dir="auto">Tenho sim, funciona bem.</div>
      <a href="/groups/5550001/posts/9999/?comment_id=9998"
         data-tip="2026年6月1日周一10:30">1天</a>
    </div>
  </div>
</div>
__DIALOG__
__THREAD_SCRIPT__
__TIP_SCRIPT__
</body></html>"""


# 关掉之后没从 DOM 里拿走、只是藏起来的旧浮层，排在文档最前面（**假设**：真站没核实过会不会留）
_STALE_DIALOG = (
    '<div role="dialog" style="display:none">'
    '<div role="button" tabindex="0" aria-label="关闭">✕</div>'
    '<div role="article"><div dir="auto">Oude laag</div>'
    '<div role="button" tabindex="0">查看更多回复</div></div></div>'
)

# 折叠点下去什么都不加载、信息流却恰好多出一条的主贴（没有评论数按钮：读不到数就「有折叠就点」）
_DEAD_FOLD_CARD = """<div role="article">
    <a href="/groups/2407063016436085/user/76/" aria-label="Noor_A"></a>
    <a href="/groups/2407063016436085/user/76/">Noor_A</a>
    <a href="/groups/2407063016436085/posts/9010/" aria-label="5天"
       data-tip="2026年5月29日周五16:00"><span>5天</span></a>
    <div data-ad-comet-preview="message">Iemand de Halo al gekoppeld aan Home Assistant?</div>
    <div role="button" tabindex="0" onclick="deadFold()">查看更多评论</div>
  </div>"""

_GHOST_CARD = """<div role="article">
    <a href="/groups/2407063016436085/user/77/" aria-label="Daan_V"></a>
    <a href="/groups/2407063016436085/user/77/">Daan_V</a>
    <a href="/groups/2407063016436085/posts/9011/" aria-label="5天"
       data-tip="2026年5月29日周五17:30"><span>5天</span></a>
    <div data-ad-comet-preview="message">Is er al een firmware-update voor de Halo 6kW?</div>
    <div role="button" tabindex="0" aria-label="发表评论">1</div>
  </div>"""


def _render(page, site, pid=None):
    """site 是 LoginSite：页面形态的开关一律处理请求时从它身上现读"""
    hidden = site.hidden_comments
    html = (page.replace("__CARD_9004__", _CARD_COMMENTS["9004"])
            .replace("__CARD_9005__", _CARD_COMMENTS["9005"])
            .replace("__COUNT_9004__", str(_THREADS["9004"]["count"] + hidden))
            .replace("__COUNT_9005__", str(_THREADS["9005"]["count"] + hidden))
            .replace("__STALE_DIALOG__", _STALE_DIALOG if site.hidden_stale_dialog else "")
            .replace("__DEAD_FOLD_CARD__", _DEAD_FOLD_CARD if site.dead_fold_card else "")
            .replace("__GHOST_CARD__", _GHOST_CARD if site.ghost_comment_card else "")
            .replace("__THREAD_SCRIPT__", _THREAD_SCRIPT)
            .replace("__TIP_SCRIPT__", _TIP_SCRIPT)
            .replace("__STICKY__", "true" if site.sticky_dialog else "false")
            .replace("__BROKEN__", "true" if site.broken_dialog else "false")
            .replace("__THREAD_URL_TAIL__", "" if site.slashless_thread_url else "/")
            .replace("__DEFERRED__", str(site.deferred_thread_comments)))
    return html.replace("__DIALOG__", _thread_dialog(pid, site)) if pid else html


_THREAD_API_RE = re.compile(r"^/api/thread/(\d+)(/more|/items)?$")
_FOLD_API_RE = re.compile(r"^/api/(fold|close)/(\w+)$")
_PERMALINK_RE = re.compile(r"^/groups/\d+/permalink/(\d+)/?$")

_CHURN_PAGE = """<html><head><meta charset="utf-8"><title>跳转中</title></head><body>
<script>window.addEventListener('load', function () {
  setTimeout(function () { location.replace('/churn?n=' + Math.random()); }, 0);
});</script>
</body></html>"""

_GROUP_RE = re.compile(r"^/groups/\d+/?$")


class _Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200, extra_headers=None):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra_headers or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _has_session(self) -> bool:
        return SESSION_COOKIE in (self.headers.get("Cookie") or "")

    def _record_lang(self):
        self.server.request_languages.append(self.headers.get("Accept-Language") or "")

    def do_GET(self):
        # 图片放在 _record_lang() 之前：浏览器取子资源时不发 Accept-Language，
        # 记进去会让「界面语言是中文」那条断言看到一堆空串
        if self.path.startswith("/media/"):
            # browser_only_media：只放行浏览器发出的子资源请求。
            # 实测（2026-08-21）Chrome 取 <img> 会带 Sec-Fetch-Dest: image + Referer +
            # sec-ch-ua，而 Playwright 的 Node 侧客户端（context.request）一个都不带 ——
            # 它压根就是另一个 HTTP 客户端，自然也不读系统代理。用这个真实差异复刻
            # 「浏览器能把图显示出来、脚本回源却拿不到」那台机器上的状态。
            browser_only = getattr(self.server, "browser_only_media", False)
            if browser_only and self.headers.get("Sec-Fetch-Dest") != "image":
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_PNG)))
            self.end_headers()
            self.wfile.write(_PNG)
            return
        self._record_lang()
        if self.path.startswith("/login"):
            self._send(_LOGIN_PAGE.format(error=""))
            return
        if self.path.startswith("/reg"):
            self._send(_REG_PAGE)
            return
        if self.path.startswith("/churn"):
            self._send(_CHURN_PAGE)
            return
        if _POST_RE.match(self.path) and self._has_session():
            self._send(_DETAIL_PAGE)
            return
        site = self.server.site
        m = _THREAD_API_RE.match(self.path)
        if m and self._has_session():
            pid, part = m.group(1), m.group(2)
            if part == "/items" and pid in _THREADS:
                self.server.thread_requests.append("items:" + pid)
                self._send(_thread_items(pid, site))
                return
            more = part == "/more"
            if pid in (_MORE_REPLIES if more else _THREADS):
                self.server.thread_requests.append(("more:" if more else "dialog:") + pid)
                if more and self.server.stale_dialog_fold:
                    self._send(_row(*_MORE_FOLD_9004))
                    return
                self._send(_row(*_MORE_REPLIES[pid]) if more else _thread_dialog(
                    pid, site, site.deferred_thread_comments))
                return
        m = _FOLD_API_RE.match(self.path)
        if m and self._has_session():
            self.server.thread_requests.append(f"{m.group(1)}:{m.group(2)}")
            self._send("")
            return
        m = _PERMALINK_RE.match(self.path)
        if m and self._has_session() and m.group(1) in _THREADS:
            self.server.thread_requests.append("permalink:" + m.group(1))
            if self.server.permalink_failure == "network":
                # 一个字节都不回就断开：浏览器报 ERR_EMPTY_RESPONSE，是「没拿到响应」那一类
                self.close_connection = True
                return
            if self.server.permalink_failure == "blocked":
                # 带正文：没正文的错误状态 Chrome 直接抛异常，走不到限流退让那条路（见 lib/http.js）
                self._send("<html><body>Tijdelijk geblokkeerd</body></html>", status=403,
                           extra_headers=[("Retry-After", str(site.permalink_retry_after))])
                return
            self._send(_render(_PERMALINK_PAGE, site, m.group(1)))
            return
        if _GROUP_RE.match(self.path):
            # 未登录时 302 到 /login/?next=... —— 2026-08-03 实测真站就是这个行为，
            # 不是给未登录用户看只读预览
            if not self._has_session():
                if self.server.landing == "reg":
                    target = "/reg/?entry_point=login&next=" + quote(self.path, safe="")
                elif self.server.landing == "churn":
                    target = "/churn"
                else:
                    target = "/login/?next=" + quote(self.path, safe="")
                self._send("", status=302, extra_headers=[("Location", target)])
                return
            self._send(_render(_FEED_PAGE, site))
            return
        self.send_error(404)

    def do_POST(self):
        self._record_lang()
        if not self.path.startswith("/login"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        email = (form.get("email") or [""])[0]
        password = (form.get("pass") or [""])[0]

        if email == TWO_FACTOR_USER:
            self._send(_TWO_FACTOR_PAGE)
            return
        if email == GOOD_USER and password == GOOD_PASSWORD:
            self._send(
                _render(_FEED_PAGE, self.server.site),
                extra_headers=[("Set-Cookie", f"{SESSION_COOKIE}=1; Path=/")],
            )
            return
        self._send(_LOGIN_PAGE.format(error=_ERROR_BOX))

    def log_message(self, *args):
        pass


class LoginSite:
    """with LoginSite() as base_url: ...

    landing="reg" 时未登录访问小组页改跳注册页，用来验证脚本会不会提示走错页面；
    landing="churn" 时改跳一个不停自我导航的页面，用来验证轮询撞上导航不会把脚本搞挂。
    browser_only_media=True 时，不带浏览器特征的图片请求一律 502 —— 复刻「Chrome 走
    系统代理能下图、脚本那条独立网络栈下不了」的真实机器状态（见 _Handler.do_GET）。
    request_languages 收下每个请求的 Accept-Language，是「浏览器界面语言」这件事
    唯一不依赖真站的观测点。
    sticky_dialog=True 时帖子浮层用关闭按钮 / Esc / 后退都关不掉，只有重新载入信息流才行。
    permalink_failure="network" 时固定链接页一个字节都不回就断开（网络类失败），
    ="blocked" 时回 403 + Retry-After（限流，秒数取 permalink_retry_after，默认 1，可在启动前改）。
    broken_dialog=True 时 9004 的浮层（信息流里点开的和固定链接页上的都算）里一查 article 就抛错。
    slashless_thread_url=True 时点折叠 pushState 的浮层地址不带末尾斜杠（2026-09-15 真站实测见过）。
    stale_dialog_fold=True 时 9004 浮层里的「查看更多回复」点了一条都不多（加载慢、或按钮不加载东西）。
    deferred_thread_comments=N（毫秒）时点折叠弹出的浮层先只有主贴，评论区 N 毫秒后才另取回来，
    负数是永远不来（真站浮层内容本来就是 XHR 取的，主贴与评论分开到是**假设**）。固定链接页不受影响。
    hidden_stale_dialog=True 时信息流页最前面藏着一个旧浮层（含 article、关闭按钮和折叠）。
    dead_fold_card=True 时多一条主贴 9010：折叠点下去自己的评论一条不多，信息流却多出一条空 article。
    ghost_comment_card=True 时多一条主贴 9011：原帖显示 1 条评论，卡片上、浮层里一条都看不到。
    tipless_reply=True 时 9004 浮层里 5603 的链接没有 tooltip，取不到时间。
    thread_requests 按顺序记下浮层接口与固定链接页被请求的次数（"dialog:9004"、
    "more:9004"、"items:9004"、"permalink:9005"、"fold:9010"）—— 「这一轮有没有去打开帖子」唯一的观测点；
    浮层是怎么关掉的也记在这里（"close:button" / "close:esc"）。
    hidden_comments 可以在两轮之间改：9004 / 9005 卡片与浮层上的原帖评论数各多出这么多条，
    浮层里却看不到 —— 复刻被「最相关」藏掉、或删了没减数的评论，条数永远对不上。
    """

    def __init__(self, port: int = 0, landing: str = "login",
                 browser_only_media: bool = False, sticky_dialog: bool = False,
                 permalink_failure: str = "", stale_dialog_fold: bool = False,
                 deferred_thread_comments: int = 0, hidden_stale_dialog: bool = False,
                 dead_fold_card: bool = False, ghost_comment_card: bool = False,
                 tipless_reply: bool = False, broken_dialog: bool = False,
                 slashless_thread_url: bool = False):
        self._port = port
        self._landing = landing
        self._browser_only_media = browser_only_media
        self._permalink_failure = permalink_failure
        self._stale_dialog_fold = stale_dialog_fold
        self._server = None
        self._thread = None
        self.request_languages = []
        self.thread_requests = []
        self.hidden_comments = 0
        self.sticky_dialog = sticky_dialog
        self.deferred_thread_comments = deferred_thread_comments
        self.hidden_stale_dialog = hidden_stale_dialog
        self.dead_fold_card = dead_fold_card
        self.ghost_comment_card = ghost_comment_card
        self.tipless_reply = tipless_reply
        self.broken_dialog = broken_dialog
        self.slashless_thread_url = slashless_thread_url
        self.permalink_retry_after = 1

    def __enter__(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
        self._server.landing = self._landing
        self._server.browser_only_media = self._browser_only_media
        self._server.permalink_failure = self._permalink_failure
        self._server.stale_dialog_fold = self._stale_dialog_fold
        # 运行中会改的值不能拷一份过去，让处理请求时现读
        self._server.site = self
        self._server.request_languages = self.request_languages
        self._server.thread_requests = self.thread_requests
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return "http://127.0.0.1:{}".format(self._server.server_address[1])

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8898
    with LoginSite(port) as base:
        print("login fixture: {}/groups/2407063016436085".format(base), flush=True)
        threading.Event().wait()
