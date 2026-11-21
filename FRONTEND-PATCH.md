# Frontend patch (manual edit)
In your existing `index.html`, find the `collectLinks` function and replace it with:

```js
function collectLinks(obj,out=[]){
 if(!obj||typeof obj!=='object')return out;
 for(const [k,v] of Object.entries(obj)){
  if(typeof v==='string' && (/^https?:\\/\\//i.test(v)||v.startsWith('/outputs/')) && (/\\.mp4(\\?|$)/i.test(v)||/download|video|file|url/i.test(k))) {
   out.push({label:k,url:v.startsWith('/')?apiBase+v:v});
  } else if(v&&typeof v==='object') collectLinks(v,out);
 }
 return out;
}
```

In the `JSON.stringify({...})` body inside the Make clips click handler, add:
`captions:$('captions').value`

The API response is now compatible with the frontend's existing polling path.
