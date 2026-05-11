#!/bin/bash
# 量化系统手动部署脚本
# 在阿里云服务器上执行

echo "=========================================="
echo "🚀 量化系统部署脚本"
echo "=========================================="

# 1. 更新系统
echo "📦 更新系统..."
apt-get update -qq

# 2. 安装Python
echo "🐍 安装Python..."
apt-get install -y -qq python3 python3-pip

# 3. 创建目录
echo "📁 创建目录..."
mkdir -p /opt/quant-system
cd /opt/quant-system

# 4. 创建requirements.txt
cat > requirements.txt << 'REQ'
fastapi==0.115.0
uvicorn[standard]==0.30.0
jinja2==3.1.4
python-multipart==0.0.9
REQ

# 5. 安装依赖
echo "📦 安装Python依赖..."
pip3 install -q -r requirements.txt
pip3 install -q tushare pandas

# 6. 创建目录结构
mkdir -p templates static data logs scripts

# 7. 提示上传文件
echo ""
echo "=========================================="
echo "⚠️  请手动上传以下文件到 /opt/quant-system/"
echo "=========================================="
echo "  - app.py"
echo "  - templates/index.html"
echo "  - templates/login.html"
echo "  - static/ (整个目录)"
echo "  - data/ (整个目录)"
echo ""
echo "上传完成后，执行以下命令启动服务："
echo ""
echo "  cd /opt/quant-system"
echo "  nohup python3 app.py > logs/app.log 2>&1 &"
echo ""
echo "然后访问: http://8.148.158.153:5001/"
echo "=========================================="
