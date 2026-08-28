Bonjour Tarek,

Je comprends ton point. Je précise effectivement mon précédent message : le téléchargement est bien une fonctionnalité native d’IBM COS.

La limitation concerne ici l’utilisation de la console Web pour les objets volumineux. La documentation IBM recommande, pour ce cas d’usage, de passer par l’IBM Cloud CLI ou l’API. Il ne s’agit donc pas de développer une fonctionnalité supplémentaire au COS, mais d’utiliser l’interface de téléchargement prévue par IBM pour les fichiers de grande taille.

Dans notre cas, nous pouvons notamment utiliser directement la commande native ibmcloud cos download, qui est également compatible Windows, plutôt que le script shell .sh transmis initialement.

Je vous propose donc de partir sur cette solution afin de débloquer les équipes.