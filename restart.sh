#!/bin/bash
# Перезапуск контейнеров на продакшене

set -e

echo "🔄 Перезапуск контейнеров magaz..."

ssh magaz@194.34.246.62 << 'ENDSSH'
cd ~/magaz
sudo docker-compose restart
sudo docker-compose ps
echo "✅ Контейнеры перезапущены"
ENDSSH
