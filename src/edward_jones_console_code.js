(async () => {
  const baseUrl = "https://www.edwardjones.com/api/v3/financial-advisor/results?q=Atlanta,%20Georgia&distance=50&distance_unit=mi&matchblock=undefined&searchtype=2&city-state-template=true";
  
  let allAdvisors = [];
  let page = 1;
  let totalPages = 1;
  const itemsPerPage = 16; // Based on response metadata

  console.log("🚀 Starting extraction...");

  do {
    console.log(`Fetching page ${page}${totalPages > 1 ? ' of ' + totalPages : ''}...`);
    
    try {
      const response = await fetch(`${baseUrl}&page=${page}`, {
        "headers": {
          "accept": "*/*",
          "accept-language": "en-US,en;q=0.9",
          "priority": "u=1, i",
          "sec-ch-ua-mobile": "?0",
          "sec-fetch-dest": "empty",
          "sec-fetch-mode": "cors",
          "sec-fetch-site": "same-origin"
        },
        "referrer": "https://www.edwardjones.com/us-en/find-a-financial-advisor/locations/georgia/atlanta",
        "method": "GET",
        "mode": "cors",
        "credentials": "include"
      });

      if (!response.ok) {
        console.error(`Failed to fetch page ${page}: HTTP ${response.status}`);
        break;
      }

      const data = await response.json();

      // On the first request, calculate total pages based on resultCount
      if (page === 1 && data.resultCount) {
        totalPages = Math.ceil(data.resultCount / (data.itemsPerPage || itemsPerPage));
        console.log(`Found ${data.resultCount} total records across ~${totalPages} pages.`);
      }

      // Append advisor array
      if (Array.isArray(data.results)) {
        allAdvisors.push(...data.results);
      } else {
        console.warn(`No results array found on page ${page}`);
      }

      page++;

      // Polite delay between requests to avoid rate limiting
      await new Promise(resolve => setTimeout(resolve, 300));

    } catch (err) {
      console.error(`Error fetching page ${page}:`, err);
      break;
    }

  } while (page <= totalPages);

  console.log(`✅ Extraction complete! Total advisors collected: ${allAdvisors.length}`);

  // Auto-download as JSON file
  if (allAdvisors.length > 0) {
    const jsonBlob = new Blob([JSON.stringify(allAdvisors, null, 2)], { type: "application/json" });
    const blobUrl = URL.createObjectURL(jsonBlob);
    
    const downloadAnchor = document.createElement("a");
    downloadAnchor.href = blobUrl;
    downloadAnchor.download = `edward_jones_advisors_atlanta_${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    document.body.removeChild(downloadAnchor);
    URL.revokeObjectURL(blobUrl);

    console.log("📥 Download started!");
  }
})();