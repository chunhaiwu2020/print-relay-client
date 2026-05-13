from PIL import Image, ImageDraw
import os

SZ = 256
img = Image.new('RGBA', (SZ, SZ), (0,0,0,0))
draw = ImageDraw.Draw(img)

# 深色圆底
r = SZ//2 - 8
draw.ellipse([8, 8, SZ-8, SZ-8], fill=(18, 22, 30, 255))

# 金色翅膀 - 左
wing = (255, 180, 50, 255)
pl = [(r, r-20), (r+30, r-60), (r+80, r-90),
      (r+60, r-50), (r+90, r-40), (r+50, r-20),
      (r+40, r+5), (r, r+30)]
draw.polygon(pl, fill=wing)
# 右翼（镜像）
pr = [(SZ-x, y) for x,y in pl]
draw.polygon(pr, fill=wing)

# 中心圆
cx, cy = SZ//2, SZ//2
draw.ellipse([cx-35, cy-35, cx+35, cy+35], fill=(18,22,30,255), outline=wing, width=3)

# 打印机图标
pc = (220, 220, 240, 255)
draw.rectangle([cx-18, cy-14, cx+18, cy+2], fill=pc)
draw.rectangle([cx-12, cy-20, cx+12, cy-14], fill=(255,255,255,255))
draw.ellipse([cx+16, cy-10, cx+22, cy-4], fill=(80, 255, 80, 255))

# 多尺寸 ICO
sizes = [256,128,64,48,32,16]
icons = [img.resize((s,s), Image.LANCZOS) for s in sizes]

path = '/out/icon.ico'
icons[0].save(path, format='ICO', sizes=[(s,s) for s in sizes])
print('OK', path, os.path.getsize(path))
