(function() {
    const visitorElement = document.getElementById("visitor-count");
    if (!visitorElement) return;

    function incrementVisitorCount() {
        fetch('/increment_visitor_count', {
            method: 'POST'
        })
        .then(response => response.json())
        .then(data => {
            visitorElement.innerText = data.visitor_count;
        })
        .catch(err => console.error('Erreur:', err));
    }
    incrementVisitorCount();
})();
