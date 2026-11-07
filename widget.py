"""HTML дашборда (тот же вид, что в чате)."""
import html


def widget_html(A, FINAL=False, wide=False):
  """wide=True: две колонки, картинка почти квадратная — Telegram показывает её на всю ширину без полос по бокам."""
  if wide: return _wide(A, FINAL)
  s=A['summary'];it=A['items'];dy=A['days'];hr=A['hours']
  day=s['updated'][:5];tm=s['updated'][6:]
  prevday=dy[-2]['day'][8:]+'.'+dy[-2]['day'][5:7] if len(dy)>1 else 'вчера'
  cmp=f'к {prevday}' if FINAL else f'к вчера на {tm}'
  f=lambda v:f"{v:,.0f}".replace(',',' ')
  sg=lambda v:('+' if v>0 else ('−' if v<0 else '±'))+f(abs(v))
  def delta(cur,prev,money=False):
    d=cur-prev
    col='var(--text-success)' if d>0 else ('var(--text-muted)' if d==0 else 'var(--text-danger)')
    pct=f' ({"+" if d>=0 else "−"}{abs(d)/prev*100:.0f}%)' if prev and money else ''
    return f'<span style="color:{col}">{sg(d)}{pct}</span> <span style="color:var(--text-muted)">{cmp}</span>'
  P='background:var(--surface-1);border-radius:10px;'
  LB='color:var(--text-secondary);font-size:11px'
  h='<div style="font-family:system-ui,-apple-system,sans-serif;color:var(--text-primary);display:grid;gap:8px;font-variant-numeric:tabular-nums;max-width:440px;font-size:13px">'
  title=f'Ozon · итог за {day}' if FINAL else f'Ozon · {day} {tm} МСК'
  h+=f'<div style="display:flex;justify-content:space-between;align-items:baseline;gap:6px"><b style="font-size:14px">{title}</b><span style="font-size:11px;color:var(--text-secondary)">посл. заказ {s["last_order"]}</span></div>'
  # hero 2 cols
  h+=(f'<div style="{P}padding:10px 12px;display:grid;grid-template-columns:1fr 1fr;gap:8px">'
      f'<div><div style="{LB}">Начислено{" за день" if FINAL else " сегодня"}</div><div style="font-size:24px;font-weight:600;line-height:1.15">{sg(s["accrued"])} ₽</div><div style="font-size:10px;color:var(--text-secondary);white-space:nowrap">продажи {f(s["sales"])} · возвр. {sg(s["returns"])}</div></div>'
      f'<div><div style="{LB}">Баланс{" на конец дня" if FINAL else ""}</div><div style="font-size:20px;font-weight:600;line-height:1.3">{f(s["balance"])} ₽</div><div style="font-size:11px;color:var(--text-secondary)">в начале дня {f(s["opening"])}</div></div></div>')
  # kpis 2x2
  def kpi(l,v,d): return f'<div style="{P}padding:7px 10px;min-width:0"><div style="{LB}">{l}</div><div style="font-size:18px;font-weight:600">{v}</div><div style="font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{d}</div></div>'
  h+='<div style="display:grid;gap:6px;grid-template-columns:1fr 1fr">'
  h+=kpi('Заказы',f(s['orders']),delta(s['orders'],s['y_orders']))
  h+=kpi('Сумма заказов',f(s['orders_sum'])+' ₽',delta(s['orders_sum'],s['y_sum'],True))
  h+=kpi(html.escape(A.get('group_label','Группа')),f(s['group']),delta(s['group'],s['y_group']))
  tot_o=s['orders']+s['cancelled']
  h+=kpi('Отмены',f(s['cancelled']),f'<span style="color:var(--text-muted)">{s["cancelled"]/tot_o*100:.0f}% от заказов</span>' if tot_o else '')
  h+='</div>'
  # hourly chart, phone-sized viewBox so text stays readable
  W,H,L,B,T=340,80,22,16,8;cur=23 if FINAL else s['hour'];mx=max(max(hr['today'][:cur+1]),max(hr['yest']),1)
  X=lambda i:L+(W-L-6)*i/23;Y=lambda v:T+(H-T-B)*(1-v/mx)
  grid=''.join(f'<line x1="{L}" x2="{W-6}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--border)" stroke-width="1"/><text class="ts" x="{L-4}" y="{Y(v)+4:.1f}" text-anchor="end">{v}</text>' for v in sorted({0,mx}))
  xt=''.join(f'<text class="ts" x="{X(i):.1f}" y="{H-2}" text-anchor="middle">{i:02d}</text>' for i in (0,6,12,18,23))
  py=' '.join(f'{X(i):.1f},{Y(v):.1f}' for i,v in enumerate(hr['yest']))
  pt=' '.join(f'{X(i):.1f},{Y(v):.1f}' for i,v in enumerate(hr['today'][:cur+1]))
  area=f'{X(0):.1f},{Y(0):.1f} '+pt+f' {X(cur):.1f},{Y(0):.1f}'
  ex,ev=X(cur),hr['today'][cur]
  svg=(f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Заказы по часам">{grid}{xt}'
       f'<polyline points="{py}" fill="none" style="stroke:var(--text-muted)" stroke-width="1.5" stroke-dasharray="4 3"/>'
       f'<polygon points="{area}" style="fill:var(--text-accent)" opacity=".12"/>'
       f'<polyline points="{pt}" fill="none" style="stroke:var(--text-accent)" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>'
       f'<circle cx="{ex:.1f}" cy="{Y(ev):.1f}" r="3.5" style="fill:var(--text-accent)"/></svg>')
  yl=prevday if FINAL else 'вчера'
  h+=(f'<div style="{P}padding:8px 10px"><div style="display:flex;justify-content:space-between;gap:4px;font-size:12px"><b>Заказы по часам</b>'
      f'<span style="color:var(--text-secondary)"><span style="color:var(--text-accent)">━</span> {"за день" if FINAL else "сегодня"} {ev} &nbsp;<span style="color:var(--text-muted)">┅</span> {yl} {hr["yest"][cur]}</span></div>{svg}</div>')
  # items: compact grid rows, no horizontal scroll
  mxo=max([r['orders'] for r in it]+[1])
  G='display:grid;grid-template-columns:minmax(0,1fr) 46px 44px 30px 46px;gap:5px;align-items:center'
  rows=f'<div style="{G};font-size:10px;color:var(--text-secondary);padding-bottom:3px"><span>Товар</span><span>Заказы</span><span style="text-align:right">Сумма</span><span style="text-align:right">Ост.</span><span style="text-align:right">Запас</span></div>'
  for r in it:
    cv=r['cover']
    if cv is None: chip='<span style="color:var(--text-muted)">—</span>'
    else:
      c='var(--text-danger)' if cv<5 else ('var(--text-warning)' if cv<10 else 'var(--text-success)')
      chip=f'<span style="display:inline-block;min-width:38px;text-align:center;padding:0 3px;border-radius:999px;border:1px solid {c};color:{c};font-size:11px">{cv if cv<100 else "99+"} дн</span>'
    cn=f'<span style="color:var(--text-danger);font-size:10px">−{r["cancelled"]}</span>' if r['cancelled'] else ''
    bw=max(3,round(18*r['orders']/mxo))
    rows+=(f'<div style="{G};border-top:1px solid var(--border);padding:3px 0">'
           f'<div style="min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px">{html.escape(r["item"])}</div>'
           f'<div style="display:flex;align-items:center;gap:4px"><div style="height:6px;border-radius:3px;background:var(--text-accent);opacity:{1 if r["orders"] else .25};width:{bw}px"></div><b>{r["orders"]}</b>{cn}</div>'
           f'<div style="text-align:right;font-size:12px">{r["sum"]/1000:.1f}k</div><div style="text-align:right;color:var(--text-secondary);font-size:12px">{f(r["stock"])}</div><div style="text-align:right">{chip}</div></div>')
  h+=f'<div style="{P}padding:8px 10px"><div style="font-size:12px;margin-bottom:4px"><b>Товары{" за день" if FINAL else " сегодня"}</b> <span style="color:var(--text-muted);font-size:10px">запас = остаток ÷ заказы в день за 7 дн.</span></div>{rows}</div>'
  # week bars
  kl=lambda v:(f'{v/1000:.0f}k' if abs(v)>=10000 else ('0' if abs(v)<50 else f'{v/1000:.1f}k')).replace('-','−')
  W2,H2=340,88;n=len(dy);mxa=max(max(r['accrued'] for r in dy),1)*1.2;bw=W2/n
  g=''
  for i,r in enumerate(dy):
    bh=max(2,(H2-38)*max(r['accrued'],0)/mxa);w=bw*.7;x=i*bw+(bw-w)/2;y=H2-28-bh;last=i==n-1
    g+=(f'<g class="{"c-blue" if last else "c-gray"}"><rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{bh:.0f}" rx="3"/></g>'
        f'<text class="{"th" if last else "ts"}" x="{x+w/2:.0f}" y="{y-4:.0f}" text-anchor="middle">{kl(r["accrued"])}</text>'
        f'<text class="ts" x="{x+w/2:.0f}" y="{H2-14}" text-anchor="middle">{r["day"][8:]}.{r["day"][5:7]}</text>'
        f'<text class="ts" x="{x+w/2:.0f}" y="{H2-1}" text-anchor="middle">{r["orders"]} з</text>')
  tot=sum(r['accrued'] for r in dy)
  note='Остаток на текущий момент.' if FINAL else 'Рекламу за день Озон списывает ночью.'
  h+=(f'<div style="{P}padding:8px 10px"><div style="display:flex;justify-content:space-between;gap:4px;font-size:12px"><b>Начислено за 7 дней</b><span style="color:var(--text-secondary)">{f(tot)} ₽ · ~{f(tot/n)}/день</span></div>'
      f'<svg viewBox="0 0 {W2} {H2}" width="100%" role="img" aria-label="Начислено по дням">{g}</svg><div style="font-size:10px;color:var(--text-muted)">{note}</div></div>')
  h+='</div>'
  return h



# ---------- широкий вид для Telegram: баланс главный, две колонки ----------
PAL = dict(card='#f3f4f6', text='#111418', sub='#4b5563', muted='#8a93a0', grid='#e5e7eb',
           accent='#2a78d6', yest='#a3abb6', bar='#d5dae1', good='#15803d', warn='#b45309', bad='#dc2626')
FONT = "font-family:Inter,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;font-variant-numeric:tabular-nums;"


def _wide(A, FINAL=False):
  P=PAL;esc=html.escape
  s=A['summary'];it=A['items'];dy=A['days'];hr=A['hours']
  day=s['updated'][:5];tm=s['updated'][6:]
  prevday=dy[-2]['day'][8:]+'.'+dy[-2]['day'][5:7] if len(dy)>1 else 'вчера'
  cmp=f'к {prevday}' if FINAL else 'к вчера'
  cur=23 if FINAL else s['hour']
  f=lambda v:f"{v:,.0f}".replace(',',' ')
  k=lambda v:(f'{v/1000:.1f}k' if abs(v)<100000 else f'{v/1000:.0f}k').replace('.0k','k').replace('-','−')
  sg=lambda v:('+' if v>0 else ('−' if v<0 else '±'))+f(abs(v))
  col=lambda d:P['good'] if d>0 else (P['bad'] if d<0 else P['muted'])
  by_code={r['code']:r['orders'] for r in it}
  gc='/'.join(str(sum(by_code.get(c,0) for c in g['codes'])) for g in A.get('caption_groups',[]))
  C=f'background:{P["card"]};border-radius:14px;padding:14px 16px;'
  # баланс и начислено за день
  left=(f'<div style="{C}">'
        f'<div style="display:flex;justify-content:space-between;color:{P["muted"]};font-size:12px"><span>Баланс</span><span>{f"итог за {day}" if FINAL else f"{day} · {tm} МСК"}</span></div>'
        f'<div style="font-size:40px;font-weight:700;letter-spacing:-.5px;line-height:1.1;margin-top:2px;white-space:nowrap">{f(s["balance"])} ₽</div>'
        f'<div style="margin-top:8px;font-size:24px;font-weight:700;color:{col(s["accrued"])};white-space:nowrap">{sg(s["accrued"])} ₽</div>'
        f'<div style="color:{P["muted"]};font-size:12px">начислено за день · продажи {f(s["sales"])}</div></div>')
  # три плитки
  do=s['orders']-s['y_orders']
  ds=s['orders_sum']-s['y_sum']
  pct=f'({"+" if ds>=0 else "−"}{abs(ds)/s["y_sum"]*100:.0f}%)' if s['y_sum'] else ''
  tiles=[('Заказы',f'{s["orders"]}'+(f' <span style="font-size:13px;color:{P["muted"]};font-weight:500">({gc})</span>' if gc else ''),
          f'<span style="color:{col(do)};font-weight:600">{sg(do)}</span> <span style="color:{P["muted"]}">{cmp}</span>'),
         ('Сумма заказов',k(s['orders_sum'])+' ₽',f'<span style="color:{col(ds)};font-weight:600">{pct}</span>'),
         ('Отмены',str(s['cancelled']),f'<span style="color:{P["muted"]}">посл. заказ {s["last_order"]}</span>')]
  left+=('<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">'
         +''.join(f'<div style="{C}padding:10px 12px;min-width:0"><div style="color:{P["muted"]};font-size:11px">{l}</div><div style="font-size:20px;font-weight:700;margin:2px 0;white-space:nowrap">{v}</div><div style="font-size:11px">{d}</div></div>' for l,v,d in tiles)
         +'</div>')
  # заказы по часам нарастающим итогом, пунктир — вчера
  W,H,L,B,T=340,215,24,18,10
  t,y=hr['today'][:cur+1],hr['yest']
  mx=max(max(t),max(y),1)
  X=lambda i:L+(W-L-8)*i/23
  Y=lambda v:T+(H-T-B)*(1-v/mx)
  g=f'<svg viewBox="0 0 {W} {H}" width="100%" style="display:block">'
  for v in sorted({0,mx}):
    g+=f'<line x1="{L}" x2="{W-8}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{P["grid"]}" stroke-width="1"/><text x="{L-6}" y="{Y(v)+4:.1f}" text-anchor="end" font-size="11" fill="{P["muted"]}">{v}</text>'
  for i in (0,6,12,18,23):
    g+=f'<text x="{X(i):.1f}" y="{H-3}" text-anchor="middle" font-size="11" fill="{P["muted"]}">{i:02d}</text>'
  py=' '.join(f'{X(i):.1f},{Y(v):.1f}' for i,v in enumerate(y))
  pt=' '.join(f'{X(i):.1f},{Y(v):.1f}' for i,v in enumerate(t))
  g+=(f'<polyline points="{py}" fill="none" stroke="{P["yest"]}" stroke-width="1.5" stroke-dasharray="4 4"/>'
      f'<polygon points="{X(0):.1f},{Y(0):.1f} {pt} {X(cur):.1f},{Y(0):.1f}" fill="{P["accent"]}" opacity=".13"/>'
      f'<polyline points="{pt}" fill="none" stroke="{P["accent"]}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>'
      f'<circle cx="{X(cur):.1f}" cy="{Y(t[-1]):.1f}" r="4" fill="{P["accent"]}" stroke="#ffffff" stroke-width="2"/></svg>')
  left+=(f'<div style="{C}"><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px"><b>Заказы по часам</b>'
         f'<span style="color:{P["muted"]}"><span style="color:{P["accent"]}">●</span> {"за день" if FINAL else "сегодня"} {t[-1]} · <span style="color:{P["yest"]}">┅</span> {prevday if FINAL else "вчера"} {y[cur]}</span></div>{g}</div>')
  # товары
  mxo=max([r['orders'] for r in it]+[1])
  G='display:grid;grid-template-columns:minmax(0,1fr) 54px 50px 34px 50px;gap:6px;align-items:center'
  rows=f'<div style="{G};font-size:11px;color:{P["muted"]};padding-bottom:4px"><span>Товар</span><span>Заказы</span><span style="text-align:right">Сумма</span><span style="text-align:right">Ост.</span><span style="text-align:right">Запас</span></div>'
  for r in it:
    cv=r['cover']
    if cv is None: chip=f'<span style="color:{P["muted"]}">—</span>'
    else:
      c=P['bad'] if cv<5 else (P['warn'] if cv<10 else P['good'])
      chip=f'<span style="display:inline-block;min-width:42px;text-align:center;padding:1px 4px;border-radius:999px;background:{c}22;color:{c};font-size:11px;font-weight:600">{f"{cv} дн" if cv<100 else "99+"}</span>'
    cn=f'<span style="color:{P["bad"]};font-size:10px"> −{r["cancelled"]}</span>' if r['cancelled'] else ''
    bar=f'<span style="display:inline-block;height:6px;border-radius:3px;background:{P["accent"]};opacity:{1 if r["orders"] else .25};width:{max(3,round(16*r["orders"]/mxo))}px;margin-right:5px;vertical-align:middle"></span>'
    rows+=(f'<div style="{G};border-top:1px solid {P["grid"]};padding:4px 0;font-size:13px">'
           f'<span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{esc(r["item"])}</span>'
           f'<span style="white-space:nowrap">{bar}<b>{r["orders"]}</b>{cn}</span><span style="text-align:right">{k(r["sum"])}</span>'
           f'<span style="text-align:right;color:{P["sub"]}">{r["stock"]}</span><span style="text-align:right">{chip}</span></div>')
  right=f'<div style="{C}"><div style="font-size:13px;margin-bottom:6px"><b>Товары {"за день" if FINAL else "сегодня"}</b></div>{rows}</div>'
  # начислено за 7 дней
  W2,H2,foot=340,120,30;n=max(len(dy),1);mxa=max([r['accrued'] for r in dy]+[1])*1.18;bw=W2/n
  w7=f'<svg viewBox="0 0 {W2} {H2}" width="100%" style="display:block">'
  for i,r in enumerate(dy):
    last=i==len(dy)-1
    bh=max(3,(H2-foot-16)*max(r['accrued'],0)/mxa);w=bw*.62;x=i*bw+(bw-w)/2;yb=H2-foot
    w7+=(f'<rect x="{x:.1f}" y="{yb-bh:.1f}" width="{w:.1f}" height="{bh:.1f}" rx="4" fill="{P["accent"] if last else P["bar"]}"/>'
         f'<text x="{x+w/2:.1f}" y="{yb-bh-5:.1f}" text-anchor="middle" font-size="11" font-weight="{600 if last else 400}" fill="{P["text"] if last else P["muted"]}">{k(r["accrued"])}</text>'
         f'<text x="{x+w/2:.1f}" y="{yb+13}" text-anchor="middle" font-size="11" fill="{P["muted"]}">{r["day"][8:]}.{r["day"][5:7]}</text>'
         f'<text x="{x+w/2:.1f}" y="{yb+27}" text-anchor="middle" font-size="10" fill="{P["muted"]}">{r["orders"]} з</text>')
  w7+='</svg>'
  right+=(f'<div style="{C}"><div style="display:flex;justify-content:space-between;font-size:12px"><b>Начислено за 7 дней</b>'
          f'<span style="color:{P["muted"]}">{f(sum(r["accrued"] for r in dy))} ₽</span></div>{w7}</div>')
  return (f'<div style="{FONT}color:{P["text"]};display:grid;grid-template-columns:1fr 1fr;gap:10px;align-items:start">'
          f'<div style="display:grid;gap:10px;min-width:0">{left}</div><div style="display:grid;gap:10px;min-width:0">{right}</div></div>')
