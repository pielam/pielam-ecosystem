docker compose up --build -d 
docker compose exec pielam_backend python manage.py makemigrations 
docker compose exec pielam_backend python manage.py migrate 

docker compose down

for data loading in new postgres db, run - 

docker compose exec pielam_backend python manage.py loaddata data_1.json
