import scraper
cars = scraper.scrape_pcamart()
print('cars:', len(cars))
imgs = [c.get('image_url') for c in cars if c.get('image_url')]
print('with images:', len(imgs))
if imgs:
    print('sample:', imgs[0])
else:
    print('NO IMAGES - first car:', cars[0] if cars else 'no cars')
