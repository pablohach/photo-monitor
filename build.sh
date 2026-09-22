#!/bin/bash


RED='\033[01;31m'
BLUE='\033[01;34m'
GREEN='\033[01;32m'
NC='\033[0m' # No Color

image="my_photo_monitor"
tag="latest"
container="photo-monitor"


while getopts t: flag
do
    case "${flag}" in
        t) tag=${OPTARG};;
    esac
done

printf "Script creación imágen ${GREEN}$image:$tag ${NC}\n"
printf "Tag: ${GREEN} $tag${NC}\n"

printf "Se parará y borrará el container ${GREEN}$container${NC}\n"
printf "Se creará la imágen ${GREEN}$image:$tag ${NC}\n"

printf "${RED}"
read -r -p "Está seguro de continuar? [S/n]" response
printf "${NC}"

response=${response,,} # tolower
if [[ $response =~ ^(s| ) ]] || [[ -z $response ]]; then
  echo "Parando container $container.."
  docker stop $container
  echo "Borrando container $container..."
  docker rm $container
  echo "Borrando imágen $image:$tag..."
  docker rmi $image:$tag
  echo "Creando imágen $image:$tag..."
  docker build --tag $image:$tag .
  
fi




