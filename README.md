docker compose up --build -d 
docker compose exec pielam_backend python manage.py makemigrations 
docker compose exec pielam_backend python manage.py migrate 

docker compose down
